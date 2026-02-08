"""Internal bot-only HTTP API for privileged Discord Bot actions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

import config

log = logging.getLogger("happy_jumper.bot_internal")

DISCORD_API_BASE = "https://discord.com/api/v10"

app = FastAPI(title="Happy Jumper Bot Internal API", version="1.0.0")


class AnnouncePayload(BaseModel):
    channel_id: int
    content: Optional[str] = None
    embed: Optional[Dict[str, Any]] = None


async def require_internal_secret(x_internal_secret: Optional[str] = Header(default=None)):
    if not config.BOT_INTERNAL_SECRET:
        raise HTTPException(status_code=500, detail="BOT_INTERNAL_SECRET is not configured")
    if x_internal_secret != config.BOT_INTERNAL_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized internal request")


def _discord_headers() -> Dict[str, str]:
    if not config.DISCORD_TOKEN:
        raise HTTPException(status_code=500, detail="DISCORD_TOKEN is not configured")
    return {"Authorization": f"Bot {config.DISCORD_TOKEN}", "Content-Type": "application/json"}


@app.get("/internal/guilds/{guild_id}/presence", dependencies=[Depends(require_internal_secret)])
async def guild_presence(guild_id: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}", headers=_discord_headers()) as resp:
            if resp.status == 200:
                return {"present": True}
            if resp.status in (403, 404):
                return {"present": False}
            detail = await resp.text()
            raise HTTPException(status_code=502, detail=f"Discord presence check failed: {detail}")


@app.get("/internal/guilds/{guild_id}/channels", dependencies=[Depends(require_internal_secret)])
async def guild_channels(guild_id: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=_discord_headers()) as resp:
            if resp.status != 200:
                detail = await resp.text()
                if resp.status in (403, 404):
                    raise HTTPException(status_code=404, detail="Guild not found or bot not in guild")
                raise HTTPException(status_code=502, detail=f"Discord channels fetch failed: {detail}")
            payload = await resp.json()

    channels: List[Dict[str, Any]] = []
    for channel in payload:
        channels.append({
            "id": str(channel["id"]),
            "name": channel.get("name", "unknown"),
            "type": channel.get("type"),
        })
    return {"channels": channels}


@app.get("/internal/guilds/{guild_id}/roles", dependencies=[Depends(require_internal_secret)])
async def guild_roles(guild_id: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles", headers=_discord_headers()) as resp:
            if resp.status != 200:
                detail = await resp.text()
                if resp.status in (403, 404):
                    raise HTTPException(status_code=404, detail="Guild not found or bot not in guild")
                raise HTTPException(status_code=502, detail=f"Discord roles fetch failed: {detail}")
            payload = await resp.json()

    roles: List[Dict[str, Any]] = []
    for role in payload:
        roles.append(
            {
                "id": str(role["id"]),
                "name": role.get("name", "unknown"),
                "position": int(role.get("position", 0)),
                "managed": bool(role.get("managed", False)),
            }
        )
    return {"roles": sorted(roles, key=lambda r: r["position"], reverse=True)}


@app.post("/internal/guilds/{guild_id}/announce", dependencies=[Depends(require_internal_secret)])
async def guild_announce(guild_id: int, request: AnnouncePayload):
    payload: Dict[str, Any] = {}
    if request.content:
        payload["content"] = request.content
    if request.embed:
        payload["embed"] = request.embed
    if not payload:
        raise HTTPException(status_code=400, detail="content or embed is required")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{DISCORD_API_BASE}/channels/{request.channel_id}/messages",
            headers=_discord_headers(),
            json=payload,
        ) as resp:
            if resp.status not in (200, 201):
                detail = await resp.text()
                raise HTTPException(status_code=502, detail=f"Discord announce failed: {detail}")
            message = await resp.json()

    return {"ok": True, "guild_id": guild_id, "channel_id": request.channel_id, "message_id": message.get("id")}


@app.get("/internal/health")
async def internal_health():
    return {"status": "healthy", "service": "bot-internal"}
