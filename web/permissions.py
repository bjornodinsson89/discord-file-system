"""
Permission checking for dashboard routes.
Requires Discord Administrator and bot presence for guild access.
"""

from fastapi import HTTPException, Request, Depends
from typing import Dict
import logging

from web.discord_api import is_bot_in_guild

log = logging.getLogger("happy_jumper.permissions")

ADMINISTRATOR_PERMISSION = 0x0000000000000008
MANAGE_GUILD_PERMISSION = 0x0000000000000020


def has_required_guild_admin_permission(permissions_str: str) -> bool:
    """Check for Administrator OR Manage Guild permissions."""
    try:
        permissions = int(permissions_str)
        has_admin = (permissions & ADMINISTRATOR_PERMISSION) == ADMINISTRATOR_PERMISSION
        has_manage_guild = (permissions & MANAGE_GUILD_PERMISSION) == MANAGE_GUILD_PERMISSION
        return has_admin or has_manage_guild
    except (ValueError, TypeError):
        return False


async def get_current_user(request: Request) -> Dict:
    """Get current authenticated user from session."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


async def get_user_guilds(user: Dict = Depends(get_current_user)) -> list:
    """Return guilds where user is admin and bot is currently present."""
    eligible_guilds = []

    for guild in user.get("guilds", []):
        is_admin = guild.get("owner", False) or has_required_guild_admin_permission(guild.get("permissions", "0"))
        if not is_admin:
            log.debug("Guild %s filtered out: user lacks admin/manage_guild", guild.get("id"))
            continue

        guild_id = int(guild.get("id", 0))
        if not guild_id:
            continue

        if await is_bot_in_guild(guild_id):
            eligible_guilds.append(guild)
        else:
            log.debug("Guild %s filtered out: bot not present", guild_id)

    return eligible_guilds


async def require_guild_admin(guild_id: int, user: Dict) -> Dict:
    """Verify user has access to guild in the filtered admin+bot-present guild list."""
    allowed_guilds = await get_user_guilds(user)
    if any(int(g["id"]) == guild_id for g in allowed_guilds):
        return user

    raise HTTPException(
        status_code=403,
        detail="You need Administrator or Manage Server permission in a server where the bot is present",
    )


async def verify_guild_access(guild_id: int, user: Dict) -> bool:
    """Helper function to verify guild access without raising exceptions."""
    try:
        await require_guild_admin(guild_id, user)
        return True
    except HTTPException:
        return False
