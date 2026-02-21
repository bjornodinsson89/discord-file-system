from __future__ import annotations

import discord

from services.casino_core.registry import get_game_registry
from services.casino_core.settings import ensure_game_defaults, get_game_config, save_game_config
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin

BET_OPTIONS = [1, 2, 5, 10, 25, 50, 100, 250, 500, 1000]
COOLDOWN_OPTIONS = [0, 1, 2, 3, 5, 10]


async def build_game_settings_embed(guild_id: int, game_key: str) -> discord.Embed:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    game_def = get_game_registry().get(game_key)
    cfg = ensure_game_defaults(game_def, get_game_config(settings, game_key))
    em = discord.Embed(title=f"{game_key.title()} Settings", color=discord.Color.dark_teal())
    em.add_field(name="Enabled", value="Yes" if cfg.get("enabled", True) else "No")
    em.add_field(name="Min Bet", value=str(int(cfg.get("min_bet") or 1)))
    em.add_field(name="Max Bet", value=str(int(cfg.get("max_bet") or 10)))
    em.add_field(name="Cooldown", value=f"{int(cfg.get('cooldown_seconds') or 0)}s")
    return em


class _MinBetSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=str(v), value=str(v)) for v in BET_OPTIONS if v <= 250]
        super().__init__(placeholder="Min bet", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, _BaseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.min_bet = int(self.values[0])
        await interaction.response.defer()


class _MaxBetSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=str(v), value=str(v)) for v in BET_OPTIONS]
        super().__init__(placeholder="Max bet", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, _BaseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.max_bet = int(self.values[0])
        await interaction.response.defer()


class _CooldownSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f"{v}s", value=str(v)) for v in COOLDOWN_OPTIONS]
        super().__init__(placeholder="Cooldown", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, _BaseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.cooldown_seconds = int(self.values[0])
        await interaction.response.defer()


class _BaseSettingsView(discord.ui.View):
    game_key: str = ""

    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.enabled: bool | None = None
        self.min_bet: int | None = None
        self.max_bet: int | None = None
        self.cooldown_seconds: int | None = None
        self.add_item(_MinBetSelect())
        self.add_item(_MaxBetSelect())
        self.add_item(_CooldownSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Toggle Enabled", style=discord.ButtonStyle.primary, row=2)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(self.guild_id)
        game_def = get_game_registry().get(self.game_key)
        cfg = ensure_game_defaults(game_def, get_game_config(settings, self.game_key))
        toggled = not bool(cfg.get("enabled", True) if self.enabled is None else self.enabled)
        self.enabled = toggled
        await interaction.response.send_message(f"Enabled set to **{toggled}**. Click Save to apply.", ephemeral=True)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(self.guild_id)
        game_def = get_game_registry().get(self.game_key)
        cfg = ensure_game_defaults(game_def, get_game_config(settings, self.game_key))
        if self.enabled is not None:
            cfg["enabled"] = self.enabled
        if self.min_bet is not None:
            cfg["min_bet"] = self.min_bet
        if self.max_bet is not None:
            cfg["max_bet"] = self.max_bet
        if self.cooldown_seconds is not None:
            cfg["cooldown_seconds"] = self.cooldown_seconds
        await save_game_config(self.guild_id, self.game_key, cfg)
        await interaction.response.edit_message(embed=await build_game_settings_embed(self.guild_id, self.game_key), view=self)


class SlotsSettingsView(_BaseSettingsView):
    game_key = "slots"


class RouletteSettingsView(_BaseSettingsView):
    game_key = "roulette"


class WheelSettingsView(_BaseSettingsView):
    game_key = "wheel"


class DiceSettingsView(_BaseSettingsView):
    game_key = "dice"
