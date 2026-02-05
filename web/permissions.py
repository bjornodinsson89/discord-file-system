"""
Permission Checking for Dashboard
Verifies user has Admin or configured admin_role in guilds.
Implements Permission Model C: Admin OR admin_role_id
"""

from fastapi import HTTPException, Request, Depends
from typing import Dict, Optional
import logging
from utils import get_database

log = logging.getLogger("happy_jumper.permissions")

# ============================================================================
# DISCORD PERMISSIONS
# ============================================================================

ADMINISTRATOR_PERMISSION = 0x0000000000000008  # Administrator permission bit


def has_administrator_permission(permissions_str: str) -> bool:
    """Check if permissions string contains Administrator permission."""
    try:
        permissions = int(permissions_str)
        return (permissions & ADMINISTRATOR_PERMISSION) == ADMINISTRATOR_PERMISSION
    except (ValueError, TypeError):
        return False


# ============================================================================
# BOT INSTANCE ACCESS
# ============================================================================

def get_bot():
    """Get the bot instance for role checking."""
    from admin_api.handlers import get_bot as _get_bot
    return _get_bot()


async def check_user_has_role(guild_id: int, user_id: int, role_id: int) -> bool:
    """Check if user has a specific role in a guild using the bot."""
    try:
        bot = get_bot()
        guild = bot.get_guild(guild_id)

        if not guild:
            guild = await bot.fetch_guild(guild_id)

        member = None
        if hasattr(guild, "get_member"):
            member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None

        if not member:
            return False

        role = None
        if hasattr(guild, "get_role"):
            role = guild.get_role(role_id)
        if not role and hasattr(guild, "fetch_roles"):
            try:
                roles = await guild.fetch_roles()
                role = next((r for r in roles if r.id == role_id), None)
            except Exception:
                role = None

        if not role:
            return False

        return role in member.roles
    except Exception as e:
        log.error(f"Error checking role: {e}")
        return False


# ============================================================================
# DEPENDENCIES
# ============================================================================

async def get_current_user(request: Request) -> Dict:
    """Get current authenticated user from session."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated"
        )
    return user


async def require_guild_admin(
    guild_id: int,
    user: Dict
) -> Dict:
    """
    Verify user has admin access to the specified guild.
    Implements Permission Model C:
    - Discord Administrator permission grants access
    - OR having the configured admin_role_id grants access
    """
    user_id = int(user.get("id", 0))
    
    # Find the guild in user's guilds
    user_guild = next(
        (g for g in user.get("guilds", []) if g["id"] == str(guild_id)),
        None
    )
    
    if not user_guild:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this server"
        )
    
    # Check 1: Discord Administrator permission
    if has_administrator_permission(user_guild.get("permissions", "0")):
        log.debug(f"User {user_id} granted access via Administrator permission")
        return user
    
    # Check 2: Configured admin_role_id
    db = get_database()
    settings = await db.get_guild_settings(guild_id)
    admin_role_id = settings.get("admin_role_id")
    
    if admin_role_id:
        if await check_user_has_role(guild_id, user_id, admin_role_id):
            log.debug(f"User {user_id} granted access via admin_role_id {admin_role_id}")
            return user
    
    # Neither condition met
    raise HTTPException(
        status_code=403,
        detail="You need Administrator permission or the configured admin role"
    )


async def get_user_guilds(user: Dict = Depends(get_current_user)) -> list:
    """Get list of guilds user can administer."""
    admin_guilds = []
    user_id = int(user.get("id", 0))
    
    db = get_database()
    
    for guild in user.get("guilds", []):
        guild_id = int(guild.get("id", 0))
        
        # Check if user has Administrator permission
        if has_administrator_permission(guild.get("permissions", "0")):
            admin_guilds.append(guild)
            continue
        
        # Check if user has configured admin_role
        try:
            settings = await db.get_guild_settings(guild_id)
            admin_role_id = settings.get("admin_role_id")
            
            if admin_role_id and await check_user_has_role(guild_id, user_id, admin_role_id):
                admin_guilds.append(guild)
        except Exception:
            # Skip guilds where we can't check settings
            pass
    
    return admin_guilds


async def verify_guild_access(
    guild_id: int,
    user: Dict
) -> bool:
    """Helper function to verify guild access without raising exceptions."""
    try:
        await require_guild_admin(guild_id, user)
        return True
    except HTTPException:
        return False
