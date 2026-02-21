from __future__ import annotations

from utils import GuildSettingsRepository, get_database

HOUSE_DEFAULTS = {
    "house_discord_id": None,
    "house_torn_id": None,
    "payouts_channel_id": None,
    "cashout_inbox_channel_id": None,
    "casino_admin_role_id": None,
}
GAME_DEFAULTS = {"enabled": True, "min_bet": 1, "max_bet": 10, "cooldown_seconds": 2}


def get_house_config(settings_row: dict) -> dict:
    cfg = dict(HOUSE_DEFAULTS)
    cfg.update((settings_row or {}).get("casino_house") or {})
    return cfg


async def update_house_config(guild_id: int, updates: dict) -> dict:
    repo = GuildSettingsRepository(get_database())
    row = await repo.get_or_create(int(guild_id))
    current = dict((row or {}).get("casino_house") or {})
    current.update({k: v for k, v in (updates or {}).items() if k in HOUSE_DEFAULTS})
    return await repo.upsert_settings(int(guild_id), casino_house=current)


def ensure_game_defaults(game_def, config: dict) -> dict:
    cfg = dict(GAME_DEFAULTS)
    cfg.update(config or {})
    if game_def:
        cfg = game_def.ensure_defaults(cfg)
    return cfg


def get_game_config(settings_row: dict, game_key: str) -> dict:
    games = dict((settings_row or {}).get("casino_games") or {})
    return dict(games.get(game_key) or {})


async def save_game_config(guild_id: int, game_key: str, config: dict) -> dict:
    repo = GuildSettingsRepository(get_database())
    row = await repo.get_or_create(int(guild_id))
    games = dict((row or {}).get("casino_games") or {})
    games[str(game_key)] = dict(config or {})
    return await repo.upsert_settings(int(guild_id), casino_games=games)
