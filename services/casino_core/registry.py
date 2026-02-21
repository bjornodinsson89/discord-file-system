from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class CasinoGameDefinition:
    key: str
    display_name: str
    description: str
    enabled_default: bool
    ensure_defaults: Callable[[dict], dict]
    build_settings_view: Callable[[Any, dict], Any]
    build_play_view: Callable[[Any, dict], Any]


def _defaults(enabled_default: bool):
    def inner(config: dict) -> dict:
        cfg = {"enabled": enabled_default, "min_bet": 1, "max_bet": 10, "cooldown_seconds": 2}
        cfg.update(config or {})
        return cfg

    return inner


def get_game_registry() -> dict[str, CasinoGameDefinition]:
    global _GAME_REGISTRY
    if _GAME_REGISTRY is not None:
        return _GAME_REGISTRY

    from views.casino_core.game_settings_panels import (
        DiceSettingsView,
        RouletteSettingsView,
        SlotsSettingsView,
        WheelSettingsView,
    )
    from views.casino_core.shared import ComingSoonView
    import discord

    def _coming_soon(_interaction: discord.Interaction, _settings_row: dict) -> discord.ui.View:
        return ComingSoonView()

    _GAME_REGISTRY = {
        "slots": CasinoGameDefinition("slots", "Slots", "Classic slots", True, _defaults(True), lambda i, s: SlotsSettingsView(i.guild_id), _coming_soon),
        "roulette": CasinoGameDefinition("roulette", "Roulette", "Roulette tables", False, _defaults(False), lambda i, s: RouletteSettingsView(i.guild_id), _coming_soon),
        "wheel": CasinoGameDefinition("wheel", "Wheel", "Lucky wheel", False, _defaults(False), lambda i, s: WheelSettingsView(i.guild_id), _coming_soon),
        "dice": CasinoGameDefinition("dice", "Dice", "Dice game", False, _defaults(False), lambda i, s: DiceSettingsView(i.guild_id), _coming_soon),
    }
    return _GAME_REGISTRY


_GAME_REGISTRY: dict[str, CasinoGameDefinition] | None = None
