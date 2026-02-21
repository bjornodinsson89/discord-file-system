from __future__ import annotations

import discord

from services.casino_core.settings import ensure_game_defaults, get_game_config, save_game_config
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin

BET_OPTIONS = ["1", "2", "5", "10", "25", "50", "100", "250", "500", "1000"]
COOLDOWN_OPTIONS = ["0", "1", "2", "3", "5", "10"]


class _GameStringSelect(discord.ui.Select):
    def __init__(self, parent: "GameSettingsView", key: str, placeholder: str, options: list[str]):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=v, value=v) for v in options],
        )
        self.parent_view = parent
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        v = int(self.values[0])
        self.parent_view.working[self.key] = v
        await interaction.response.edit_message(embed=self.parent_view.build_embed(), view=self.parent_view)


class GameSettingsView(discord.ui.View):
    def __init__(self, guild_id: int, game_key: str, initial: dict):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.game_key = game_key
        self.working = dict(initial)
        self.add_item(_GameStringSelect(self, "min_bet", "Select minimum bet", BET_OPTIONS[:8]))
        self.add_item(_GameStringSelect(self, "max_bet", "Select maximum bet", BET_OPTIONS))
        self.add_item(_GameStringSelect(self, "cooldown_seconds", "Select cooldown (seconds)", COOLDOWN_OPTIONS))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    def build_embed(self) -> discord.Embed:
        em = discord.Embed(title=f"{self.game_key.title()} Settings", color=discord.Color.orange())
        em.add_field(name="Enabled", value="Yes" if self.working.get("enabled", True) else "No")
        em.add_field(name="Min Bet", value=str(int(self.working.get("min_bet") or 1)))
        em.add_field(name="Max Bet", value=str(int(self.working.get("max_bet") or 1)))
        em.add_field(name="Cooldown", value=f"{int(self.working.get('cooldown_seconds') or 0)}s", inline=False)
        return em

    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        self.working["enabled"] = not bool(self.working.get("enabled", True))
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=3)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await save_game_config(self.guild_id, self.game_key, self.working)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


async def build_game_settings_view(guild_id: int, game_key: str) -> GameSettingsView:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    initial = ensure_game_defaults(None, get_game_config(settings, game_key))
    return GameSettingsView(guild_id, game_key, initial)
