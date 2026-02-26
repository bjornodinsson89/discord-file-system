from __future__ import annotations

import asyncio
import heapq
import math
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_database, get_pool
from utils import GuildSettingsRepository, get_security_manager, get_torn_api
from utils.embeds import create_info_embed, create_error_embed
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError

BANK_MAX_AMOUNT = 2_000_000_000
CACHE_TTL_SECONDS = 3600
STALE_MAX_AGE_SECONDS = 21600
BUCKET_SIZE = 1_000_000
DURATIONS_DAYS = {"1w": 7, "2w": 14, "1m": 30, "2m": 60, "3m": 90}


def parse_money(text: str) -> int:
    cleaned = (text or "").strip().lower().replace("$", "").replace(",", "")
    if not cleaned:
        raise ValueError("Amount is required.")

    multiplier = 1
    if cleaned.endswith("b"):
        multiplier = 1_000_000_000
        cleaned = cleaned[:-1]
    elif cleaned.endswith("m"):
        multiplier = 1_000_000
        cleaned = cleaned[:-1]

    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        raise ValueError("Invalid amount format.")

    value = float(cleaned) * multiplier
    return int(round(value))


def parse_yes_no(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if normalized in {"yes", "true", "on", "1"}:
        return True
    if normalized in {"no", "false", "off", "0", ""}:
        return False
    raise ValueError("TCI must be yes/no/true/false.")


def compute_profit(amount: int, apr_percent: float, days: int, merits: int, tci: bool) -> tuple[int, int, float]:
    merits_mult = 1 + 0.5 * (merits / 10)
    tci_mult = 1.1 if tci else 1.0
    total_mult = merits_mult * tci_mult
    apr_effective = (apr_percent / 100.0) * total_mult
    profit_ratio = (apr_effective / 365.0) * days
    profit_ratio_rounded = round(profit_ratio, 4)
    total_profit = int(round(profit_ratio_rounded * amount, 0))
    profit_per_day = int(round(total_profit / days, 0))
    return total_profit, profit_per_day, apr_effective * 100.0


def planner_to_goal(start_amount: int, rates: dict[str, float], merits: int, tci: bool) -> tuple[int, list[dict[str, Any]]]:
    if start_amount >= BANK_MAX_AMOUNT:
        return 0, []

    pq: list[tuple[int, int, list[dict[str, Any]]]] = []
    heapq.heappush(pq, (0, start_amount, []))
    best_days_for_bucket: dict[int, int] = {}

    while pq:
        days_so_far, amount, path = heapq.heappop(pq)
        if amount >= BANK_MAX_AMOUNT:
            return days_so_far, path

        for duration, duration_days in DURATIONS_DAYS.items():
            apr = float(rates.get(duration) or 0.0)
            profit, _, _ = compute_profit(amount, apr, duration_days, merits, tci)
            next_amount = min(BANK_MAX_AMOUNT, amount + profit)
            next_days = days_so_far + duration_days
            next_bucket = next_amount // BUCKET_SIZE
            best = best_days_for_bucket.get(next_bucket)
            if best is not None and next_days >= best:
                continue
            best_days_for_bucket[next_bucket] = next_days
            step = {
                "duration": duration,
                "days": duration_days,
                "start": amount,
                "profit": profit,
                "end": next_amount,
            }
            heapq.heappush(pq, (next_days, next_amount, path + [step]))

    raise ValueError("Unable to find investment path.")


class BankCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._repo = GuildSettingsRepository(get_database())
        self._cache: dict[int, dict[str, Any]] = {}
        self._cache_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._cache_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[guild_id] = lock
        return lock

    async def _get_rates(self, guild_id: int, api_key: str) -> dict[str, float]:
        now = time.monotonic()
        cached = self._cache.get(guild_id)
        if cached and (now - float(cached.get("fetched_at", 0))) < CACHE_TTL_SECONDS:
            return cached["rates"]

        async with self._get_lock(guild_id):
            now = time.monotonic()
            cached = self._cache.get(guild_id)
            if cached and (now - float(cached.get("fetched_at", 0))) < CACHE_TTL_SECONDS:
                return cached["rates"]

            try:
                rates = await get_torn_api().get_bank_rates(api_key)
                normalized = {k: float(v) for k, v in rates.items() if k in DURATIONS_DAYS}
                if not normalized:
                    raise TornAPIError("Torn bank rates were empty.")
                self._cache[guild_id] = {"rates": normalized, "fetched_at": time.monotonic()}
                return normalized
            except (TornAPIError, TornAPIPermissionError, TornAPIRateLimitError):
                if cached:
                    age = now - float(cached.get("fetched_at", 0))
                    if age <= STALE_MAX_AGE_SECONDS:
                        return cached["rates"]
                raise

    @app_commands.command(name="bank_calc", description="Calculate best Torn bank investment options")
    @app_commands.describe(
        amount="Starting amount (e.g. 2b, 500m, 2,000,000,000)",
        merits="Bank merits (0 to 10)",
        tci="Use TCI bonus? yes/no/true/false",
    )
    @app_commands.choices(
        tci=[
            app_commands.Choice(name="No", value="no"),
            app_commands.Choice(name="Yes", value="yes"),
            app_commands.Choice(name="True", value="true"),
            app_commands.Choice(name="False", value="false"),
        ]
    )
    async def bank_calc(
        self,
        interaction: discord.Interaction,
        amount: str,
        merits: app_commands.Range[int, 0, 10] = 0,
        tci: app_commands.Choice[str] | None = None,
    ):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=create_error_embed("Unavailable", "This command can only be used in a server."),
                ephemeral=True,
            )
            return

        tci_raw = tci.value if tci else "no"
        try:
            starting_amount = parse_money(amount)
            tci_enabled = parse_yes_no(tci_raw)
        except ValueError as exc:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid input", str(exc)),
                ephemeral=False,
            )
            return

        if starting_amount <= 0:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid amount", "Amount must be greater than 0."),
                ephemeral=False,
            )
            return

        settings = await self._repo.get_or_create(guild.id)
        encrypted_key = settings.get("bank_rates_api_key_encrypted")
        if not encrypted_key:
            await interaction.response.send_message(
                embed=create_error_embed(
                    "Not configured",
                    "Bank rates API key is not configured yet. Ask an admin to set it in `/setup` → Feature Toggles.",
                ),
                ephemeral=False,
            )
            return

        try:
            api_key = get_security_manager().decrypt_api_key(str(encrypted_key))
        except Exception:
            await interaction.response.send_message(
                embed=create_error_embed("Configuration error", "Stored bank rates API key could not be read. Ask an admin to re-save it in setup."),
                ephemeral=False,
            )
            return

        try:
            rates = await self._get_rates(guild.id, api_key)
        except TornAPIPermissionError:
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", "The configured bank rates API key does not have required Torn access."),
                ephemeral=False,
            )
            return
        except TornAPIRateLimitError:
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", "Torn API is rate limited right now. Try again shortly."),
                ephemeral=False,
            )
            return
        except TornAPIError:
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", "Could not fetch bank rates from Torn right now. Please try again later."),
                ephemeral=False,
            )
            return

        invest_amount = min(starting_amount, BANK_MAX_AMOUNT)
        excess_amount = max(0, starting_amount - BANK_MAX_AMOUNT)

        rows: list[tuple[str, int, int, float]] = []
        best_duration = None
        best_per_day = -math.inf
        for duration in ["1w", "2w", "1m", "2m", "3m"]:
            apr = float(rates.get(duration) or 0.0)
            days = DURATIONS_DAYS[duration]
            total_profit, per_day, eff_apr = compute_profit(invest_amount, apr, days, merits, tci_enabled)
            rows.append((duration, total_profit, per_day, eff_apr))
            if per_day > best_per_day:
                best_per_day = per_day
                best_duration = (duration, total_profit, per_day)

        path_days, path_steps = planner_to_goal(invest_amount, rates, merits, tci_enabled)
        cycles = len(path_steps)
        shown_steps = path_steps[:8]
        step_lines = [
            f"{idx}. {step['duration']} ({step['days']}d): ${step['start']:,} + ${step['profit']:,} = ${step['end']:,}"
            for idx, step in enumerate(shown_steps, start=1)
        ]
        if cycles > 8:
            step_lines.append("...")
            final_step = path_steps[-1]
            step_lines.append(
                f"{cycles}. {final_step['duration']} ({final_step['days']}d): ${final_step['start']:,} + ${final_step['profit']:,} = ${final_step['end']:,}"
            )

        table_lines = ["Dur  Days   APR%   EffAPR%      Profit   Profit/Day"]
        for duration, total_profit, per_day, eff_apr in rows:
            days = DURATIONS_DAYS[duration]
            apr = float(rates.get(duration) or 0.0)
            table_lines.append(
                f"{duration:<3} {days:>4} {apr:>6.2f} {eff_apr:>9.2f} ${total_profit:>11,} ${per_day:>11,}"
            )

        description = (
            f"**A) ⭐ Best duration right now:** `{best_duration[0]}`\n"
            f"Profit: **${best_duration[1]:,}** | Profit/day: **${best_duration[2]:,}**\n\n"
            f"**B) All durations**\n"
            f"```\n" + "\n".join(table_lines) + "\n```\n"
            f"**C) Quickest path to $2,000,000,000 (reinvest-only)**\n"
            f"ETA: **{path_days} days** | Cycles: **{cycles}**\n"
            + ("\n".join(step_lines) if step_lines else "Already at bank cap.")
        )

        embed = create_info_embed("Bank Investment Calculator", description)
        embed.add_field(name="Starting Amount", value=f"${starting_amount:,}", inline=True)
        embed.add_field(name="Merits", value=str(merits), inline=True)
        embed.add_field(name="TCI", value="Yes" if tci_enabled else "No", inline=True)
        embed.add_field(name="Bank Cap", value=f"${BANK_MAX_AMOUNT:,}", inline=True)
        if excess_amount > 0:
            embed.add_field(name="Excess Not Invested", value=f"${excess_amount:,}", inline=True)
        embed.set_footer(text="Assumes current APR snapshot, Torn rounding rules, immediate reinvest at maturity, and 1h cached rates per server.")
        await interaction.response.send_message(embed=embed, ephemeral=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog(bot))
