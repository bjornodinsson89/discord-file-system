from __future__ import annotations

import json

from utils import GuildSettingsRepository, get_database

HOUSE_DEFAULTS = {
    "house_discord_id": None,
    "house_torn_id": None,
    "payout_proof_channel_id": None,
    "big_wins_channel_id": None,
    "casino_admin_role_id": None,
}
GAME_DEFAULTS = {"enabled": True, "min_bet": 1, "max_bet": 10, "cooldown_seconds": 2}


def _coerce_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def get_house_config(settings_row: dict) -> dict:
    cfg = dict(HOUSE_DEFAULTS)
    raw = _coerce_dict((settings_row or {}).get("casino_house"))
    cfg.update(raw)
    return cfg


async def update_house_config(guild_id: int, updates: dict) -> dict:
    repo = GuildSettingsRepository(get_database())
    row = await repo.get_or_create(int(guild_id))
    current = _coerce_dict((row or {}).get("casino_house"))
    current.update({k: v for k, v in (updates or {}).items() if k in HOUSE_DEFAULTS})
    return await repo.upsert_settings(int(guild_id), casino_house=current)


def ensure_game_defaults(game_def, config: dict) -> dict:
    cfg = dict(GAME_DEFAULTS)
    cfg.update(config or {})
    if game_def:
        cfg = game_def.ensure_defaults(cfg)
    return cfg


def get_game_config(settings_row: dict, game_key: str) -> dict:
    games_raw = _coerce_dict((settings_row or {}).get("casino_games"))
    game_raw = _coerce_dict(games_raw.get(game_key))
    return game_raw


async def save_game_config(guild_id: int, game_key: str, config: dict) -> dict:
    repo = GuildSettingsRepository(get_database())
    row = await repo.get_or_create(int(guild_id))
    games = _coerce_dict((row or {}).get("casino_games"))
    games[str(game_key)] = _coerce_dict(config)
    return await repo.upsert_settings(int(guild_id), casino_games=games)
