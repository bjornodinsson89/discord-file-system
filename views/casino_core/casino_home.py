from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from services.casino_core.registry import get_game_registry
from views.casino_core.cashout_panel import CashoutRequestModal
from views.casino_core.deposit_panel import DepositPanelView, deposit_panel_embed
from views.casino_core.ledger_panel import send_admin_ledger_panel

from utils.database import get_pool


class GameSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=v.display_name, value=v.key, description=f"{v.description} (coming soon)") for v in get_game_registry().values()]
        super().__init__(placeholder="Choose game", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if key == "slots":
            from cogs.casino import open_slots_panel

            await open_slots_panel(interaction)
            return
        await interaction.response.send_message(
            f"{get_game_registry()[key].display_name} coming soon.",
            ephemeral=True,
            view=get_game_registry()[key].build_play_view(interaction, {}),
        )


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
        await send_admin_ledger_panel(interaction, self.guild_id)

    @discord.ui.button(label="Game Settings", style=discord.ButtonStyle.secondary)
    async def game_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)


async def casino_home_embed(guild_id: int, discord_id: int) -> discord.Embed:
    wallet = await CasinoCoreRepository(get_pool()).get_wallet(guild_id, discord_id)
    bal = int((wallet or {}).get("balance_tokens") or 0)
    em = discord.Embed(title="Casino", description="Core panel", color=discord.Color.gold())
    em.add_field(name="Balance", value=f"**{bal}** tokens")
    return em
