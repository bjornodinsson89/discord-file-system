from __future__ import annotations

import discord

from views.casino_core.admin_credit import AdminCreditModal
from views.casino_core.game_settings_panels import (
    DiceSettingsView,
    RouletteSettingsView,
    SlotsSettingsView,
    WheelSettingsView,
)
from views.casino_core.house_settings import HouseSettingsView, house_settings_embed
from views.casino_core.ledger_panel import send_admin_ledger_panel
from views.casino_core.permissions import ensure_casino_admin


async def back_of_house_embed(guild_id: int) -> discord.Embed:
    em = discord.Embed(title="Back of House", description="Casino admin controls", color=discord.Color.dark_gold())
    em.set_footer(text=f"Guild {guild_id}")
    return em


class BackOfHouseView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Casino Settings", style=discord.ButtonStyle.primary)
    async def casino_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message(
            embed=await house_settings_embed(self.guild_id),
            view=HouseSettingsView(self.guild_id),
            ephemeral=True,
        )

    @discord.ui.button(label="Slots Settings", style=discord.ButtonStyle.secondary)
    async def slots_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message("Slots settings", view=SlotsSettingsView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Roulette Settings", style=discord.ButtonStyle.secondary)
    async def roulette_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message("Roulette settings", view=RouletteSettingsView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Wheel Settings", style=discord.ButtonStyle.secondary)
    async def wheel_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message("Wheel settings", view=WheelSettingsView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Dice Settings", style=discord.ButtonStyle.secondary)
    async def dice_settings(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_message("Dice settings", view=DiceSettingsView(self.guild_id), ephemeral=True)

    @discord.ui.button(label="Ledger", style=discord.ButtonStyle.success)
    async def ledger(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await send_admin_ledger_panel(interaction, self.guild_id)

    @discord.ui.button(label="Admin Credit", style=discord.ButtonStyle.success)
    async def admin_credit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_modal(AdminCreditModal(self.guild_id))

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
