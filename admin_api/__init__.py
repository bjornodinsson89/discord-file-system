"""
Admin API Module
FastAPI routes and handlers for the dashboard.
"""

from admin_api.routes import (
    guild_router,
    sessions_router,
    raffles_router,
    insurance_router,
    settings_router,
    audit_router,
    stats_router,
)

from admin_api.handlers import (
    set_bot_instance,
    get_bot,
    create_session_handler,
    list_sessions_handler,
    create_raffle_handler,
    list_raffles_handler,
    create_policy_handler,
    update_session_message,
    update_raffle_message,
)

__all__ = [
    # Routers
    'guild_router',
    'sessions_router',
    'raffles_router',
    'insurance_router',
    'settings_router',
    'audit_router',
    'stats_router',
    # Handlers
    'set_bot_instance',
    'get_bot',
    'create_session_handler',
    'list_sessions_handler',
    'create_raffle_handler',
    'list_raffles_handler',
    'create_policy_handler',
    'update_session_message',
    'update_raffle_message',
]
