"""
Discord OAuth2 Authentication
Handles Discord login, token exchange, and session management.
"""

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import RedirectResponse
import aiohttp
import secrets
from typing import Optional, Dict
import logging

import config
from web.permissions import get_current_user

log = logging.getLogger("happy_jumper.auth")

router = APIRouter()

# ============================================================================
# DISCORD OAuth2 CONFIGURATION
# ============================================================================

DISCORD_API_BASE = "https://discord.com/api/v10"
OAUTH_SCOPES = ["identify", "guilds"]
OAUTH_URL = (
    f"https://discord.com/oauth2/authorize"
    f"?client_id={config.DISCORD_CLIENT_ID}"
    f"&redirect_uri={config.OAUTH_REDIRECT_URI}"
    f"&response_type=code"
    f"&scope={'%20'.join(OAUTH_SCOPES)}"
)

# ============================================================================
# OAUTH FLOW
# ============================================================================

@router.get("/login")
async def login(request: Request):
    """Redirect to Discord OAuth."""
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    return RedirectResponse(f"{OAUTH_URL}&state={state}")


@router.get("/callback")
async def callback(request: Request, code: str, state: str):
    """Handle OAuth callback from Discord."""
    # Verify state to prevent CSRF
    session_state = request.session.get("oauth_state")
    if not session_state or session_state != state:
        raise HTTPException(status_code=400, detail="Invalid state parameter")
    
    # Exchange code for token
    async with aiohttp.ClientSession() as session:
        # Get access token
        token_data = {
            "client_id": config.DISCORD_CLIENT_ID,
            "client_secret": config.DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.OAUTH_REDIRECT_URI,
        }
        
        async with session.post(
            f"{DISCORD_API_BASE}/oauth2/token",
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ) as resp:
            if resp.status != 200:
                log.error(f"Token exchange failed: {await resp.text()}")
                raise HTTPException(status_code=400, detail="Failed to exchange code")
            
            token_response = await resp.json()
            access_token = token_response["access_token"]
        
        # Get user info
        async with session.get(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to get user info")
            
            user_data = await resp.json()
        
        # Get user's guilds
        async with session.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to get guilds")
            
            guilds_data = await resp.json()
    
    # Store session data
    request.session["user"] = {
        "id": user_data["id"],
        "username": user_data["username"],
        "discriminator": user_data.get("discriminator", "0"),
        "avatar": user_data.get("avatar"),
        "guilds": [
            {
                "id": g["id"],
                "name": g["name"],
                "icon": g.get("icon"),
                "owner": g.get("owner", False),
                "permissions": g.get("permissions", "0")
            }
            for g in guilds_data
        ]
    }
    
    # Redirect to dashboard
    return RedirectResponse(url=f"{config.FRONTEND_URL}/")


@router.get("/logout")
async def logout(request: Request):
    """Clear user session."""
    request.session.clear()
    return RedirectResponse(url=f"{config.FRONTEND_URL}/login")


@router.get("/me")
async def get_me(user: Dict = Depends(get_current_user)):
    """Get current user info."""
    return user


@router.get("/status")
async def auth_status(request: Request):
    """Check if user is authenticated."""
    user = request.session.get("user")
    return {
        "authenticated": user is not None,
        "user": user if user else None
    }
