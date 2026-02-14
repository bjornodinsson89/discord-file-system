from __future__ import annotations

from .database import init_pool, get_pool, close_pool
from .torn_api import TornAPIClient, init_torn_api, get_torn_api
from .security import SecurityManager, init_security, get_security_manager
from .guild_settings_repository import GuildSettingsRepository

# Backward-compatible aliases
init_database = init_pool
get_database = get_pool

__all__ = [
    "init_pool",
    "get_pool",
    "close_pool",
    "init_database",
    "get_database",
    "TornAPIClient",
    "init_torn_api",
    "get_torn_api",
    "SecurityManager",
    "init_security",
    "get_security_manager",
    "GuildSettingsRepository",
]
