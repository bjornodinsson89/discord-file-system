"""Internal bot-only HTTP API for privileged Discord Bot actions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import config

DISCORD_API_BASE = "https://discord.com/api/v10"

app = FastAPI(title="Happy Jumper Bot Internal API", version="1.0.0")


@app.on_event("startup")
async def startup_validation() -> None:
    config.validate_config()


class AnnouncePayload(BaseModel):
    channel_id: int
    content: Optional[str] = None
    embed: Optional[Dict[str, Any]] = None


def _error(status_code: int, detail: str, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"detail": detail, "code": code})


async def require_internal_secret(x_internal_secret: Optional[str] = Header(default=None)):
    if not config.BOT_INTERNAL_SECRET:
        raise _error(500, "BOT_INTERNAL_SECRET is not configured", "missing_internal_secret")
    if x_internal_secret != config.BOT_INTERNAL_SECRET:
        raise _error(401, "Unauthorized internal request", "unauthorized_internal_request")


def _discord_headers() -> Dict[str, str]:
    if not config.DISCORD_TOKEN:
        raise _error(500, "DISCORD_TOKEN is not configured", "missing_discord_token")
    return {"Authorization": f"Bot {config.DISCORD_TOKEN}", "Content-Type": "application/json"}


async def _discord_request(method: str, path: str, json: Dict[str, Any] | None = None) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{DISCORD_API_BASE}{path}", headers=_discord_headers(), json=json) as resp:
            if resp.content_type == "application/json":
                payload: Any = await resp.json()
            else:
                payload = await resp.text()
            return resp.status, payload


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException):
    if isinstance(exc.detail, dict) and "detail" in exc.detail and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc.detail), "code": "http_error"})


@app.get("/internal/health", dependencies=[Depends(require_internal_secret)])
async def internal_health():
    return {"status": "ok", "service": "bot-internal"}


@app.get("/internal/guilds/{guild_id}/presence", dependencies=[Depends(require_internal_secret)])
async def guild_presence(guild_id: int):
    status, payload = await _discord_request("GET", f"/guilds/{guild_id}")
    if status == 200:
        return {"present": True}
    if status in (403, 404):
        return {"present": False}
    raise _error(502, f"Discord presence check failed: {payload}", "discord_presence_failed")


@app.get("/internal/guilds/{guild_id}/channels", dependencies=[Depends(require_internal_secret)])
async def guild_channels(guild_id: int):
    status, payload = await _discord_request("GET", f"/guilds/{guild_id}/channels")
    if status in (403, 404):
        raise _error(404, "Guild not found or bot not in guild", "guild_not_accessible")
    if status != 200:
        raise _error(502, f"Discord channels fetch failed: {payload}", "discord_channels_failed")

    channels: List[Dict[str, Any]] = []
    for channel in payload:
        channels.append({
            "id": str(channel["id"]),
            "name": channel.get("name", "unknown"),
            "type": channel.get("type"),
            "position": int(channel.get("position", 0)),
        })
    return {"channels": channels}


@app.get("/internal/guilds/{guild_id}/roles", dependencies=[Depends(require_internal_secret)])
async def guild_roles(guild_id: int):
    status, payload = await _discord_request("GET", f"/guilds/{guild_id}/roles")
    if status in (403, 404):
        raise _error(404, "Guild not found or bot not in guild", "guild_not_accessible")
    if status != 200:
        raise _error(502, f"Discord roles fetch failed: {payload}", "discord_roles_failed")

    roles: List[Dict[str, Any]] = []
    for role in payload:
        roles.append({
            "id": str(role["id"]),
            "name": role.get("name", "unknown"),
            "position": int(role.get("position", 0)),
            "type": 0,
        })
    return {"roles": sorted(roles, key=lambda r: r["position"], reverse=True)}


@app.post("/internal/guilds/{guild_id}/announce", dependencies=[Depends(require_internal_secret)])
async def guild_announce(guild_id: int, request: AnnouncePayload):
    payload: Dict[str, Any] = {}
    if request.content:
        payload["content"] = request.content
    if request.embed:
        payload["embed"] = request.embed
    if not payload:
        raise _error(400, "content or embed is required", "invalid_announce_payload")

    status, message = await _discord_request("POST", f"/channels/{request.channel_id}/messages", json=payload)
    if status not in (200, 201):
        raise _error(502, f"Discord announce failed: {message}", "discord_announce_failed")

    return {"ok": True, "guild_id": guild_id, "channel_id": request.channel_id, "message_id": message.get("id")}
