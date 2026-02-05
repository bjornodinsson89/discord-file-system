"""
Web Module
FastAPI app, OAuth, and permissions.
"""

from web.app import app
from web.auth import router as auth_router
from web.permissions import (
    get_current_user,
    require_guild_admin,
    get_user_guilds,
    verify_guild_access,
)

__all__ = [
    'app',
    'auth_router',
    'get_current_user',
    'require_guild_admin',
    'get_user_guilds',
    'verify_guild_access',
]
