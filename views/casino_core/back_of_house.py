from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from repositories.casino_core import CasinoCoreRepository
from repositories.users import UsersRepository
from services.casino_games.slots import SLOTS_JACKPOT_POOL_KEY
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin

ACCOUNTING_RESERVE_TARGET = 5000

async def _build_accounting_embed(guild_id: int, viewer_discord_id: int, days: int, label: str) -> discord.Embed:
    now_utc = datetime.now(timezone.utc)
    if int(days) == 1:
        start_utc = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
        end_utc = now_utc
    else:
        start_utc = now_utc - timedelta(days=int(days))
        end_utc = now_utc

    users_repo = UsersRepository(get_database())
    user_row = await users_repo.get_user_api_key(int(viewer_discord_id))
    tz_name = (user_row or {}).get("timezone_name") or "UTC"
    try:
        viewer_tz = ZoneInfo(tz_name)
    except Exception:
        viewer_tz = ZoneInfo("UTC")
        tz_name = "UTC"

    repo = CasinoCoreRepository(get_database())
    totals = await repo.fetch_slots_accounting_totals_window(
        int(guild_id),
        start_utc=start_utc,
        end_utc=end_utc,
    )
    async with repo.acquire() as conn:
        jackpot_row = await repo.get_or_create_pool(
            conn,
            guild_id=int(guild_id),
            pool_key=SLOTS_JACKPOT_POOL_KEY,
            seed_tokens=0,
            seed_millis=0,
        )

    wagers = int(totals.get("wagers") or 0)
    payouts = int(totals.get("payouts") or 0)
    jackpot_contrib = int(totals.get("jackpot_contrib") or 0)
    jackpot_admin_add = int(totals.get("jackpot_admin_add") or 0)
    overflow = int(totals.get("jackpot_overflow_to_house") or 0)
    profit = int(wagers - payouts - jackpot_contrib - jackpot_admin_add + overflow)
    distributable = max(0, int(profit - ACCOUNTING_RESERVE_TARGET))
    split_each = distributable // 3
    pool_tokens = int(jackpot_row.get("tokens") or 0)
    start_local = start_utc.astimezone(viewer_tz)
    end_local = end_utc.astimezone(viewer_tz)
    utc_window = f"{start_utc.strftime('%Y-%m-%d %H:%M')} → {end_utc.strftime('%Y-%m-%d %H:%M')}"
    local_window = f"{start_local.strftime('%Y-%m-%d %H:%M')} → {end_local.strftime('%Y-%m-%d %H:%M')}"

    em = discord.Embed(
        title="Back of House Accounting",
        description="\n".join(
            [
                f"Window: **{label}**",
                f"Window (UTC): `{utc_window}`",
                f"Window (Your TZ: {tz_name}): `{local_window}`",
            ]
        ),
        color=discord.Color.dark_teal(),
    )
    em.add_field(name="Wagers (W)", value=f"`{wagers}`", inline=True)
    em.add_field(name="Payouts (P)", value=f"`{payouts}`", inline=True)
    em.add_field(name="Jackpot Contrib (Jc)", value=f"`{jackpot_contrib}`", inline=True)
    em.add_field(name="Admin Jackpot Adds (Ja)", value=f"`{jackpot_admin_add}`", inline=True)
    em.add_field(name="Overflow to House (Ov)", value=f"`{overflow}`", inline=True)
    em.add_field(name="Profit", value=f"`{profit}`", inline=True)
    em.add_field(name="Current Jackpot Pool", value=f"`{pool_tokens}`", inline=True)
    em.add_field(name="Reserve Target", value=f"`{ACCOUNTING_RESERVE_TARGET}`", inline=True)
    em.add_field(name="Distributable", value=f"`{distributable}`", inline=True)
    em.add_field(name="Split Each (3 admins)", value=f"`{split_each}`", inline=True)
    em.set_footer(text="No payouts are automated by this report.")
    return em


async def back_of_house_embed(guild_id: int) -> discord.Embed:
    em = discord.Embed(
        title="Back of House",
        description="Casino admin controls",
        color=discord.Color.dark_gold(),
    )
    casino_repo = CasinoCoreRepository(get_database())
    async with casino_repo.acquire() as conn:
        jackpot_row = await casino_repo.get_or_create_pool(
            conn,
            guild_id=int(guild_id),
            pool_key=SLOTS_JACKPOT_POOL_KEY,
            seed_tokens=0,
            seed_millis=0,
        )
    em.add_field(name="Slots Jackpot (Max Bet)", value=f"`{int(jackpot_row.get('tokens') or 0)}`", inline=False)
    em.set_footer(text=f"Guild {guild_id}")
    return em


async def casino_settings_embed(guild_id: int) -> discord.Embed:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    casino_repo = CasinoCoreRepository(get_database())
    async with casino_repo.acquire() as conn:
        seed_row = await casino_repo.get_or_create_slots_server_seed(conn, int(guild_id), for_update=False)
        jackpot_row = await casino_repo.get_or_create_pool(
            conn,
            guild_id=int(guild_id),
            pool_key=SLOTS_JACKPOT_POOL_KEY,
            seed_tokens=0,
            seed_millis=0,
        )
    em = discord.Embed(
        title="Casino Settings",
        description="Casino-wide controls",
        color=discord.Color.orange(),
    )
    em.add_field(name="Casino Enabled", value="Yes" if settings.get("casino_enabled") else "No", inline=False)
    em.add_field(name="Slots Server Seed Hash", value=f"`{seed_row.get('server_seed_hash')}`", inline=False)
    em.add_field(name="Slots Jackpot (Max Bet)", value=f"`{int(jackpot_row.get('tokens') or 0)}`", inline=False)
    return em


class CasinoSettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Toggle Casino Enabled", style=discord.ButtonStyle.success)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        repo = GuildSettingsRepository(get_database())
        row = await repo.get_or_create(self.guild_id)
        await repo.upsert_settings(self.guild_id, casino_enabled=not bool(row.get("casino_enabled")))
        await interaction.response.edit_message(
            embed=await casino_settings_embed(self.guild_id),
            view=CasinoSettingsView(self.guild_id),
        )


class AddJackpotSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        options = [
            discord.SelectOption(label=str(amount), value=str(amount), description=f"Add {amount} tokens")
            for amount in range(10, 101, 10)
        ]
        super().__init__(placeholder="Add to Jackpot", min_values=1, max_values=1, options=options, row=4)
        self.guild_id = int(guild_id)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        amount = int(self.values[0])
        await interaction.response.defer(ephemeral=True)
        repo = CasinoCoreRepository(get_database())
        async with repo.acquire() as conn:
            async with conn.transaction():
                row = await repo.add_to_pool(
                    conn,
                    guild_id=int(self.guild_id),
                    pool_key=SLOTS_JACKPOT_POOL_KEY,
                    add_tokens=int(amount),
                    add_millis=0,
                )
                await conn.execute(
                    """
                    INSERT INTO casino_slots_accounting (
                        guild_id,
                        actor_discord_id,
                        bet,
                        payout,
                        win_type,
                        jackpot_admin_add,
                        jackpot_pool_before,
                        jackpot_pool_after
                    ) VALUES ($1, $2, 0, 0, 'admin', $3, $4, $5)
                    """,
                    int(self.guild_id),
                    int(interaction.user.id),
                    int(amount),
                    int((row.get("tokens") or 0) - amount),
                    int(row.get("tokens") or 0),
                )
        pool_tokens = int(row.get("tokens") or 0)
        await interaction.followup.send(
            f"✅ Added {amount} to jackpot. Jackpot (Max Bet) is now {pool_tokens}.",
            ephemeral=True,
        )


class BackOfHouseView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(AddJackpotSelect(guild_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="House / Admin Settings", style=discord.ButtonStyle.primary, row=0)
    async def house_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.house_settings import HouseSettingsView, house_settings_embed

        await interaction.response.edit_message(
            embed=await house_settings_embed(self.guild_id),
            view=HouseSettingsView(self.guild_id),
        )

    @discord.ui.button(label="Casino Settings", style=discord.ButtonStyle.primary, row=0)
    async def casino_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.edit_message(
            embed=await casino_settings_embed(self.guild_id),
            view=CasinoSettingsView(self.guild_id),
        )

    @discord.ui.button(label="Slots Settings", style=discord.ButtonStyle.secondary, row=1)
    async def slots_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.game_settings_panels import SlotsSettingsView, build_game_settings_embed

        await interaction.response.send_message(
            embed=await build_game_settings_embed(self.guild_id, "slots"),
            view=SlotsSettingsView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Roulette Settings", style=discord.ButtonStyle.secondary, row=1)
    async def roulette_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.game_settings_panels import RouletteSettingsView, build_game_settings_embed

        await interaction.response.send_message(
            embed=await build_game_settings_embed(self.guild_id, "roulette"),
            view=RouletteSettingsView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Wheel Settings", style=discord.ButtonStyle.secondary, row=1)
    async def wheel_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.game_settings_panels import WheelSettingsView, build_game_settings_embed

        await interaction.response.send_message(
            embed=await build_game_settings_embed(self.guild_id, "wheel"),
            view=WheelSettingsView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Dice Settings", style=discord.ButtonStyle.secondary, row=2)
    async def dice_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.game_settings_panels import DiceSettingsView, build_game_settings_embed

        await interaction.response.send_message(
            embed=await build_game_settings_embed(self.guild_id, "dice"),
            view=DiceSettingsView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Ledger", style=discord.ButtonStyle.success, row=2)
    async def ledger(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.ledger_panel import send_admin_ledger_panel

        await send_admin_ledger_panel(interaction, self.guild_id)

    @discord.ui.button(label="Admin Credit", style=discord.ButtonStyle.success, row=2)
    async def admin_credit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.admin_credit import AdminCreditView

        await interaction.response.send_message("Admin Credit", view=AdminCreditView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Accounting", style=discord.ButtonStyle.success, row=2)
    async def accounting(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message(
            embed=await _build_accounting_embed(self.guild_id, interaction.user.id, 1, "Today"),
            view=AccountingWindowView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Rotate Slots Seed", style=discord.ButtonStyle.danger, row=3)
    async def rotate_slots_seed(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        repo = CasinoCoreRepository(get_database())
        async with repo.acquire() as conn:
            async with conn.transaction():
                row = await repo.rotate_slots_server_seed(conn, int(self.guild_id))
        await interaction.response.send_message(
            "\n".join([
                "✅ Slots seed rotated.",
                f"New Server Seed Hash: `{row.get('server_seed_hash')}`",
                f"Previous Server Seed: `{row.get('previous_server_seed')}`",
                f"Previous Server Seed Hash: `{row.get('previous_server_seed_hash')}`",
                "Use previous seed + client seed + nonce to verify older spins.",
            ]),
            ephemeral=True,
        )

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=3)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Closed.", view=self)


class AccountingWindowSelect(discord.ui.Select):
    def __init__(self, guild_id: int):
        super().__init__(
            placeholder="Choose accounting window",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Today", value="1"),
                discord.SelectOption(label="Last 7 days", value="7"),
                discord.SelectOption(label="Last 30 days", value="30"),
            ],
        )
        self.guild_id = int(guild_id)

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild_id or int(interaction.guild_id) != int(self.guild_id):
            await interaction.response.send_message("❌ This panel belongs to a different server.", ephemeral=True)
            return
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        days = int(self.values[0])
        label = "Today" if days == 1 else f"Last {days} days"
        await interaction.response.edit_message(
            embed=await _build_accounting_embed(self.guild_id, interaction.user.id, days, label),
            view=AccountingWindowView(self.guild_id),
        )


class AccountingWindowView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = int(guild_id)
        self.add_item(AccountingWindowSelect(guild_id))
