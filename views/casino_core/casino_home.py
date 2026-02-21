from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from services.casino_core.registry import GAME_REGISTRY
from views.casino_core.cashout_panel import CashoutRequestModal
from views.casino_core.deposit_panel import DepositPanelView, deposit_panel_embed
from views.casino_core.game_settings_panels import DiceSettingsView, RouletteSettingsView, SlotsSettingsView, WheelSettingsView
from views.casino_core.ledger_panel import AdminLedgerView

from utils.database import get_pool


class GameSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=v.display_name, value=v.key, description=f"{v.description} (coming soon)") for v in GAME_REGISTRY.values()]
        super().__init__(placeholder="Choose game", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await interaction.response.send_message(f"{GAME_REGISTRY[key].display_name} coming soon.", ephemeral=True, view=GAME_REGISTRY[key].build_play_view(interaction, {}))


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
        v = AdminLedgerView(self.guild_id)
        await interaction.response.send_message("Casino ledger", view=v, ephemeral=True)
        await v.render(interaction)

    @discord.ui.button(label="Game Settings", style=discord.ButtonStyle.secondary)
    async def game_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        msg = "Choose game settings command: /jump casino_slots_settings, /jump casino_roulette_settings, /jump casino_wheel_settings, /jump casino_dice_settings"
        await interaction.response.send_message(msg, ephemeral=True)


async def casino_home_embed(guild_id: int, discord_id: int) -> discord.Embed:
    wallet = await CasinoCoreRepository(get_pool()).get_wallet(guild_id, discord_id)
    bal = int((wallet or {}).get("balance_tokens") or 0)
    em = discord.Embed(title="Casino", description="Core panel", color=discord.Color.gold())
    em.add_field(name="Balance", value=f"**{bal}** tokens")
    return em
