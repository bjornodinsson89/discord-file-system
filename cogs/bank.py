from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from utils.database import get_database
from utils import GuildSettingsRepository
from services.admin_key_pool import AdminKeyPoolService
from utils.embeds import create_info_embed, create_error_embed
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError

BANK_MAX_AMOUNT = 2_000_000_000
CACHE_TTL_SECONDS = 3600
STALE_MAX_AGE_SECONDS = 21600
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


def time_to_goal_same_duration(
    start_amount: int,
    goal: int,
    apr_percent: float,
    days: int,
    merits: int,
    tci: bool,
) -> dict[str, float | int | bool]:
    amount = min(start_amount, goal)
    total_days = 0
    cycles = 0
    hit_safety_limit = False

    while amount < goal:
        cycles += 1
        if cycles > 5000:
            hit_safety_limit = True
            break
        total_profit, _, _ = compute_profit(amount, apr_percent, days, merits, tci)
        amount += total_profit
        total_days += days

    return {
        "total_days": total_days,
        "cycles": cycles,
        "years": round(total_days / 365.0, 1),
        "final_amount": amount,
        "hit_safety_limit": hit_safety_limit,
    }


class BankCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._repo = GuildSettingsRepository(get_database())
        self._admin_key_pool = AdminKeyPoolService()
        self._cache: dict[int, dict[str, Any]] = {}
        self._cache_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._cache_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._cache_locks[guild_id] = lock
        return lock

    async def _get_rates(self, guild: discord.Guild) -> dict[str, float]:
        now = time.monotonic()
        guild_id = guild.id
        cached = self._cache.get(guild_id)
        if cached and (now - float(cached.get("fetched_at", 0))) < CACHE_TTL_SECONDS:
            return cached["rates"]

        async with self._get_lock(guild_id):
            now = time.monotonic()
            cached = self._cache.get(guild_id)
            if cached and (now - float(cached.get("fetched_at", 0))) < CACHE_TTL_SECONDS:
                return cached["rates"]

            try:
                rates = await self._admin_key_pool.get_bank_rates_for_guild(guild)
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
                ephemeral=True,
            )
            return

        if starting_amount <= 0:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid amount", "Amount must be greater than 0."),
                ephemeral=True,
            )
            return

        await self._repo.get_or_create(guild.id)

        try:
            rates = await self._get_rates(guild)
        except TornAPIPermissionError:
            settings = await self._repo.get_or_create(guild.id)
            description = (
                "Bank rates are unavailable because the selected admin key does not have the required Torn access."
                if settings.get("admin_key_strategy") == "single"
                else "Bank rates are unavailable because no admin key with the required Torn access is available."
            )
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", description),
                ephemeral=True,
            )
            return
        except TornAPIRateLimitError:
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", "Torn API is rate limited right now. Try again shortly."),
                ephemeral=True,
            )
            return
        except TornAPIError as exc:
            message = str(exc).strip().lower()
            if "no single admin key is configured for this server" in message:
                description = "Bank rates are unavailable because no single admin key is configured for this server."
            elif "selected admin has no stored torn api key" in message:
                description = "Bank rates are unavailable because the selected admin has no stored Torn API key."
            elif "selected admin is no longer eligible" in message or "selected admin is no longer in this server" in message:
                description = "Bank rates are unavailable because the selected admin is no longer eligible for setup/admin access in this server."
            elif "no admin api keys are available for this server" in message:
                description = "Bank rates are unavailable because no admin in this server has a stored Torn API key."
            else:
                description = "Could not fetch bank rates from Torn right now. Please try again later."
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", description),
                ephemeral=True,
            )
            return

        invest_amount = min(starting_amount, BANK_MAX_AMOUNT)
        excess_amount = max(0, starting_amount - BANK_MAX_AMOUNT)

        rows: list[tuple[str, int, int, float]] = []
        best_duration: tuple[str, int, int, float, int] | None = None
        for duration in ["1w", "2w", "1m", "2m", "3m"]:
            apr = float(rates.get(duration) or 0.0)
            days = DURATIONS_DAYS[duration]
            total_profit, per_day, eff_apr = compute_profit(invest_amount, apr, days, merits, tci_enabled)
            rows.append((duration, total_profit, per_day, eff_apr))
            if (
                best_duration is None
                or per_day > best_duration[2]
                or (per_day == best_duration[2] and total_profit > best_duration[1])
                or (per_day == best_duration[2] and total_profit == best_duration[1] and days < best_duration[4])
            ):
                best_duration = (duration, total_profit, per_day, eff_apr, days)

        table_lines = ["Dur EffAPR%       Profit       /day"]
        for duration, total_profit, per_day, eff_apr in rows:
            is_best = best_duration is not None and duration == best_duration[0]
            star = " ⭐" if is_best else ""
            table_lines.append(f"{duration:<2} {eff_apr:>7.2f}% ${total_profit:>12,} ${per_day:>10,}{star}")

        timeline_rows: list[dict[str, str | int | float | bool]] = []
        for duration in ["1w", "2w", "1m", "2m", "3m"]:
            duration_days = DURATIONS_DAYS[duration]
            apr = float(rates.get(duration) or 0.0)
            sim = time_to_goal_same_duration(
                start_amount=invest_amount,
                goal=BANK_MAX_AMOUNT,
                apr_percent=apr,
                days=duration_days,
                merits=merits,
                tci=tci_enabled,
            )
            timeline_rows.append(
                {
                    "duration": duration,
                    "days": duration_days,
                    "total_days": int(sim["total_days"]),
                    "cycles": int(sim["cycles"]),
                    "years": float(sim["years"]),
                    "hit_safety_limit": bool(sim["hit_safety_limit"]),
                }
            )

        timeline_rows.sort(key=lambda item: int(item["total_days"]))
        c_lines = ["Time to $2B (reinvest-only, same duration)"]
        for idx, item in enumerate(timeline_rows):
            marker = "⭐ " if idx == 0 else "   "
            if item["hit_safety_limit"]:
                c_lines.append(
                    f"{marker}{item['duration']}: safety stop (>5000 cycles)"
                )
                continue
            c_lines.append(
                f"{marker}{item['duration']}: {item['total_days']}d (~{item['years']:.1f}y) | {item['cycles']} cycles"
            )

        if best_duration is None:
            await interaction.response.send_message(
                embed=create_error_embed("Bank rates unavailable", "No valid bank durations were returned."),
                ephemeral=True,
            )
            return

        description = (
            f"⭐ Best duration: {best_duration[0]}\n"
            f"Profit: ${best_duration[1]:,} | Profit/day: ${best_duration[2]:,} | Eff APR: {best_duration[3]:.2f}%\n"
            f"```text\n" + "\n".join(table_lines) + "\n```\n"
            "```text\n" + "\n".join(c_lines[:6]) + "\n```"
        )

        embed = create_info_embed("Bank Investment Calculator", description)
        embed.add_field(name="Starting Amount", value=f"${starting_amount:,}", inline=True)
        embed.add_field(name="Merits", value=f"{merits}/10", inline=True)
        embed.add_field(name="TCI", value="Yes" if tci_enabled else "No", inline=True)
        embed.add_field(name="Bank Cap", value=f"${BANK_MAX_AMOUNT:,}", inline=True)
        if excess_amount > 0:
            embed.add_field(name="Excess Not Invested", value=f"${excess_amount:,}", inline=True)
        embed.set_footer(text="Assumes current APR snapshot, Torn rounding rules, immediate reinvest at maturity, 1h cached rates.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(BankCog(bot))
