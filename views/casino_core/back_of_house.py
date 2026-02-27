from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from services.casino_games.slots import SLOTS_JACKPOT_POOL_KEY
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin


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
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.house_settings import HouseSettingsView, house_settings_embed

        await interaction.response.edit_message(
            embed=await house_settings_embed(self.guild_id),
            view=HouseSettingsView(self.guild_id),
        )

    @discord.ui.button(label="Casino Settings", style=discord.ButtonStyle.primary, row=0)
    async def casino_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.edit_message(
            embed=await casino_settings_embed(self.guild_id),
            view=CasinoSettingsView(self.guild_id),
        )

    @discord.ui.button(label="Slots Settings", style=discord.ButtonStyle.secondary, row=1)
    async def slots_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
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
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.ledger_panel import send_admin_ledger_panel

        await send_admin_ledger_panel(interaction, self.guild_id)

    @discord.ui.button(label="Admin Credit", style=discord.ButtonStyle.success, row=2)
    async def admin_credit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        from views.casino_core.admin_credit import AdminCreditView

        await interaction.response.send_message("Admin Credit", view=AdminCreditView(self.guild_id), ephemeral=True)


    @discord.ui.button(label="Rotate Slots Seed", style=discord.ButtonStyle.danger, row=3)
    async def rotate_slots_seed(self, interaction: discord.Interaction, _: discord.ui.Button):
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
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Closed.", view=self)
