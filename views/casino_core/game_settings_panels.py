from __future__ import annotations

import json

import discord

from services.casino_core.settings import ensure_game_defaults, get_game_config, save_game_config
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin


class _ConfigModal(discord.ui.Modal):
    enabled = discord.ui.TextInput(label="enabled (true/false)", required=True, max_length=5)
    min_bet = discord.ui.TextInput(label="min_bet", required=True, max_length=12)
    max_bet = discord.ui.TextInput(label="max_bet", required=True, max_length=12)
    cooldown_seconds = discord.ui.TextInput(label="cooldown_seconds", required=True, max_length=12)
    extras = discord.ui.TextInput(label="extras JSON", required=False, style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, guild_id: int, game_key: str):
        super().__init__(title=f"{game_key.title()} Settings")
        self.guild_id = guild_id
        self.game_key = game_key

    async def on_submit(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(self.guild_id)
        base = ensure_game_defaults(None, get_game_config(settings, self.game_key))
        base["enabled"] = str(self.enabled.value).strip().lower() in {"true", "1", "yes", "on"}
        base["min_bet"] = int(self.min_bet.value)
        base["max_bet"] = int(self.max_bet.value)
        base["cooldown_seconds"] = int(self.cooldown_seconds.value)
        if self.game_key == "roulette":
            base.setdefault("wheel_type", "european")
            base.setdefault("allowed_bets", [])
            base.setdefault("max_payout", 10)
        elif self.game_key == "wheel":
            base.setdefault("slices", [])
            base.setdefault("max_payout", 10)
        elif self.game_key == "dice":
            base.setdefault("mode", "roll_under")
            base.setdefault("max_payout", 10)
        if str(self.extras.value).strip():
            base.update(json.loads(str(self.extras.value)))
        await save_game_config(self.guild_id, self.game_key, base)
        await interaction.response.send_message("✅ Game settings saved.", ephemeral=True)


class _BaseSettingsView(discord.ui.View):
    game_key: str = ""

    def __init__(self, guild_id: int):
        super().__init__(timeout=180)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Open Settings Modal", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_modal(_ConfigModal(self.guild_id, self.game_key))


class SlotsSettingsView(_BaseSettingsView):
    game_key = "slots"


class RouletteSettingsView(_BaseSettingsView):
    game_key = "roulette"


class WheelSettingsView(_BaseSettingsView):
    game_key = "wheel"


class DiceSettingsView(_BaseSettingsView):
    game_key = "dice"
