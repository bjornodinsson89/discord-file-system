"""HTTP client for web -> bot internal API communication."""

from __future__ import annotations

from typing import Any, Dict, List

import aiohttp
from fastapi import HTTPException

import config


class InternalBotClient:
    def __init__(self):
        self.base_url = config.BOT_SERVICE_URL
        self.secret = config.BOT_INTERNAL_SECRET

    def _headers(self) -> Dict[str, str]:
        if not self.secret:
            raise HTTPException(status_code=500, detail="BOT_INTERNAL_SECRET is not configured")
        return {"X-Internal-Secret": self.secret}

    async def _request(self, method: str, path: str, json: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if not self.base_url:
            raise HTTPException(status_code=500, detail="BOT_SERVICE_URL is not configured")

        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=10)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers(), json=json) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status >= 400:
                        detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
                        raise HTTPException(status_code=502, detail=f"Bot internal service error: {detail}")
                    return payload if isinstance(payload, dict) else {}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="Bot internal service offline")

    async def guild_presence(self, guild_id: int) -> bool:
        data = await self._request("GET", f"/internal/guilds/{guild_id}/presence")
        return bool(data.get("present", False))

    async def guild_channels(self, guild_id: int) -> List[Dict[str, Any]]:
        data = await self._request("GET", f"/internal/guilds/{guild_id}/channels")
        return data.get("channels", [])

    async def guild_roles(self, guild_id: int) -> List[Dict[str, Any]]:
        data = await self._request("GET", f"/internal/guilds/{guild_id}/roles")
        return data.get("roles", [])


bot_internal_client = InternalBotClient()
