from __future__ import annotations

__all__ = [
    "init_pool",
    "get_pool",
    "close_pool",
    "init_database",
    "get_database",
    "is_initialized",
    "wait_until_initialized",
    "TornAPIClient",
    "init_torn_api",
    "get_torn_api",
    "SecurityManager",
    "init_security",
    "get_security_manager",
    "GuildSettingsRepository",
    "require_api_key",
    "has_api_key",
]


def __getattr__(name: str):
    if name in {
        "init_pool",
        "get_pool",
        "close_pool",
        "init_database",
        "get_database",
        "is_initialized",
        "wait_until_initialized",
    }:
        from . import database

        if name == "init_database":
            return database.init_pool
        return getattr(database, name)

    if name in {"TornAPIClient", "init_torn_api", "get_torn_api"}:
        from . import torn_api

        return getattr(torn_api, name)

    if name in {"SecurityManager", "init_security", "get_security_manager"}:
        from . import security

        return getattr(security, name)

    if name == "GuildSettingsRepository":
        from .guild_settings_repository import GuildSettingsRepository

        return GuildSettingsRepository

    if name in {"require_api_key", "has_api_key"}:
        from . import guards

        return getattr(guards, name)

    raise AttributeError(name)
