"""Discord REST helpers for web service-safe guild access."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

import config

log = logging.getLogger("happy_jumper.discord_api")

DISCORD_API_BASE = "https://discord.com/api/v10"
_CACHE_TTL_SECONDS = 300
_presence_cache: dict[int, tuple[bool, float]] = {}


def _bot_headers() -> Dict[str, str]:
    if not config.DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is not configured")
    return {"Authorization": f"Bot {config.DISCORD_TOKEN}"}


def _cache_get(guild_id: int) -> Optional[bool]:
    cached = _presence_cache.get(guild_id)
    if not cached:
        return None
    is_present, expires_at = cached
    if time.time() >= expires_at:
        _presence_cache.pop(guild_id, None)
        return None
    return is_present


def _cache_put(guild_id: int, value: bool):
    _presence_cache[guild_id] = (value, time.time() + _CACHE_TTL_SECONDS)


async def is_bot_in_guild(guild_id: int) -> bool:
    """Check bot membership with GET /guilds/{guild_id}; 200=present, 403/404=not present."""
    cached = _cache_get(guild_id)
    if cached is not None:
        return cached

    try:
        headers = _bot_headers()
    except RuntimeError:
        return False

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}", headers=headers) as resp:
            if resp.status == 200:
                _cache_put(guild_id, True)
                return True
            if resp.status in (403, 404):
                _cache_put(guild_id, False)
                return False
            body = await resp.text()
            log.warning("Unexpected guild presence response for %s: %s %s", guild_id, resp.status, body)
            _cache_put(guild_id, False)
            return False


async def get_guild(guild_id: int) -> Optional[Dict[str, Any]]:
    """Return guild metadata from Discord REST or None when unavailable."""
    try:
        headers = _bot_headers()
    except RuntimeError:
        return None

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}?with_counts=true", headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status in (403, 404):
                return None
            body = await resp.text()
            log.warning("Failed to fetch guild %s metadata: %s %s", guild_id, resp.status, body)
            return None


async def get_guild_channels(guild_id: int) -> List[Dict[str, Any]]:
    """Return text channels from Discord REST."""
    headers = _bot_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                if resp.status in (403, 404):
                    raise RuntimeError("Bot cannot access this guild's channels. Re-invite the bot with proper permissions.")
                log.warning("Failed to fetch channels for guild %s: %s %s", guild_id, resp.status, body)
                raise RuntimeError("Discord API failed while loading channels. Please retry.")

            payload = await resp.json()
            channels = []
            for channel in payload:
                if channel.get("type") == 0:
                    channels.append({
                        "id": str(channel["id"]),
                        "name": channel.get("name", "unknown"),
                        "type": "text",
                    })
            return channels


async def get_guild_roles(guild_id: int) -> List[Dict[str, Any]]:
    """Return guild roles from Discord REST."""
    headers = _bot_headers()

    async with aiohttp.ClientSession() as session:
        async with session.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/roles", headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                if resp.status in (403, 404):
                    raise RuntimeError("Bot cannot access this guild's roles. Re-invite the bot with proper permissions.")
                log.warning("Failed to fetch roles for guild %s: %s %s", guild_id, resp.status, body)
                raise RuntimeError("Discord API failed while loading roles. Please retry.")

            payload = await resp.json()
            roles = []
            for role in payload:
                if role.get("name") == "@everyone":
                    continue
                if role.get("managed"):
                    continue
                roles.append({
                    "id": str(role["id"]),
                    "name": role.get("name", "unknown"),
                    "color": str(role.get("color", 0)),
                })
            return sorted(roles, key=lambda r: r["name"].lower())


class DiscordRestClient:
    """Minimal Discord REST client for sending/editing webhook-like bot messages."""

    def __init__(self):
        self._headers = _bot_headers()

    async def send_message(
        self,
        channel_id: int,
        *,
        content: Optional[str] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
                headers=self._headers,
                json=payload,
            ) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                body = await resp.text()
                raise RuntimeError(f"Failed to send Discord message ({resp.status}): {body}")

    async def edit_message(
        self,
        channel_id: int,
        message_id: int,
        *,
        content: Optional[str] = None,
        embeds: Optional[List[Dict[str, Any]]] = None,
        components: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if components is not None:
            payload["components"] = components

        async with aiohttp.ClientSession() as session:
            async with session.patch(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
                headers=self._headers,
                json=payload,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = await resp.text()
                raise RuntimeError(f"Failed to edit Discord message ({resp.status}): {body}")

    async def delete_message(self, channel_id: int, message_id: int):
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{DISCORD_API_BASE}/channels/{channel_id}/messages/{message_id}",
                headers=self._headers,
            ) as resp:
                if resp.status == 204:
                    return
                body = await resp.text()
                raise RuntimeError(f"Failed to delete Discord message ({resp.status}): {body}")
