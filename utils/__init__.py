from __future__ import annotations

from .database import init_pool, get_pool, close_pool, get_database, is_initialized, wait_until_initialized
from .torn_api import TornAPIClient, init_torn_api, get_torn_api
from .security import SecurityManager, init_security, get_security_manager
from .guild_settings_repository import GuildSettingsRepository
from .guards import require_api_key, has_api_key

# Backward-compatible alias
init_database = init_pool

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
