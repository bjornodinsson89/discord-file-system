from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from services.casino_core.registry import get_game_registry
from services.casino_games.slots import CasinoSlotsService, SlotsError
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from views.casino_core.cashout_panel import CashoutRequestModal
from views.casino_core.deposit_panel import DepositPanelView, deposit_panel_embed
from views.casino_core.ledger_panel import send_admin_ledger_panel
from views.casino_core.permissions import ensure_casino_admin


class GameSelect(discord.ui.Select):
    def __init__(self):
        try:
            from views.casino_games.slots_play import SlotsPlayView  # noqa: F401

            has_slots = True
        except Exception:
            has_slots = False

        options: list[discord.SelectOption] = []
        for definition in get_game_registry().values():
            if definition.key == "slots":
                description = "Play slots" if has_slots else f"{definition.description} (coming soon)"
            else:
                description = f"{definition.description} (coming soon)"
            options.append(discord.SelectOption(label=definition.display_name, value=definition.key, description=description))
        super().__init__(placeholder="Choose game", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key != "slots":
            await interaction.response.send_message(
                f"{get_game_registry()[key].display_name} coming soon.",
                ephemeral=True,
                view=get_game_registry()[key].build_play_view(interaction, {}),
            )
            return

        if not interaction.guild_id:
            await interaction.response.send_message("Guild only command.", ephemeral=True)
            return

        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        get_house_config(settings)
        if not settings.get("casino_enabled"):
            await interaction.response.send_message(
                "Casino is disabled. Ask admins to run /back_of_house and enable casino.",
                ephemeral=True,
            )
            return

        try:
            from views.casino_games.slots_play import SlotsPlayView
        except Exception:
            await interaction.response.send_message("Slots module not installed yet.", ephemeral=True)
            return

        service = CasinoSlotsService(get_pool())
        config = await service.ensure_slots_config(interaction.guild_id)
        if not config.get("enabled", True):
            await interaction.response.send_message("Slots is disabled in this server.", ephemeral=True)
            return

        try:
            snapshot = await service.get_balance_and_pool(interaction.guild_id, interaction.user.id)
        except SlotsError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        view = SlotsPlayView(
            guild_id=interaction.guild_id,
            discord_id=interaction.user.id,
            service=service,
            config=snapshot["config"],
            balance=int(snapshot["balance"]),
            pool_tokens=int(snapshot["pool_tokens"]),
            pool_millis=int(snapshot["pool_millis"]),
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class CasinoHomeView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.repo = CasinoCoreRepository(get_pool())
        self.add_item(GameSelect())

    @discord.ui.button(label="Deposit", style=discord.ButtonStyle.success)
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(embed=await deposit_panel_embed(self.guild_id), view=DepositPanelView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Cashout", style=discord.ButtonStyle.primary)
    async def cashout(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(CashoutRequestModal(self.guild_id))

    @discord.ui.button(label="Ledger", style=discord.ButtonStyle.secondary)
    async def ledger(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await send_admin_ledger_panel(interaction, self.guild_id)

    @discord.ui.button(label="Game Settings", style=discord.ButtonStyle.secondary)
    async def game_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)


async def casino_home_embed(guild_id: int, discord_id: int) -> discord.Embed:
    wallet = await CasinoCoreRepository(get_pool()).get_wallet(guild_id, discord_id)
    bal = int((wallet or {}).get("balance_tokens") or 0)
    em = discord.Embed(title="Casino", description="Core panel", color=discord.Color.gold())
    em.add_field(name="Balance", value=f"**{bal}** tokens")
    return em
