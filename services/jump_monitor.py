from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from utils import get_database, get_security_manager, get_torn_api

log = logging.getLogger("happy_jumper.services.jump_monitor")


class JumpMonitor:
    """In-memory live status monitor for jump sessions."""

    def __init__(self, poll_interval_seconds: int = 10):
        self.poll_interval_seconds = poll_interval_seconds
        self._tasks: dict[int, asyncio.Task] = {}
        self._statuses: dict[int, dict[int, dict[str, Any]]] = {}
        self._needs_refresh: set[int] = set()

    async def start(self, jump_id: int) -> None:
        if jump_id in self._tasks and not self._tasks[jump_id].done():
            return

        self._tasks[jump_id] = asyncio.create_task(self._poll_loop(jump_id))

    async def stop(self, jump_id: int) -> None:
        task = self._tasks.pop(jump_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._statuses.pop(jump_id, None)
        self._needs_refresh.discard(jump_id)

    def get_status(self, jump_id: int) -> dict[int, dict[str, Any]]:
        return self._statuses.get(jump_id, {})

    def mark_needs_refresh(self, jump_id: int) -> None:
        self._needs_refresh.add(jump_id)

    async def _poll_loop(self, jump_id: int) -> None:
        while True:
            try:
                keep_running = await self._poll_once(jump_id)
                if not keep_running:
                    self._tasks.pop(jump_id, None)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Jump monitor poll failed for jump_id=%s", jump_id)

            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self, jump_id: int) -> bool:
        db = get_database()
        async with db.pool.acquire() as conn:
            session = await conn.fetchrow("SELECT * FROM happy_jump_sessions WHERE id = $1", jump_id)
            if not session:
                return False

            status = session.get("status")
            if status not in {"open", "locked"}:
                return False

            signup_rows = await conn.fetch(
                "SELECT discord_id FROM happy_jump_signups WHERE session_id = $1", jump_id
            )

        participant_ids = {int(row["discord_id"]) for row in signup_rows}
        participant_ids.add(int(session["host_discord_id"]))

        torn_api = get_torn_api()
        security = get_security_manager()

        jump_status = self._statuses.setdefault(jump_id, {})
        for discord_id in participant_ids:
            try:
                key_data = await db.get_user_api_key(discord_id)
                if not key_data:
                    jump_status[discord_id] = {
                        "energy_current": None,
                        "drug_cd": None,
                        "booster_cd": None,
                        "ready_bool": False,
                        "no_api_key": True,
                        "updated_at": datetime.now(timezone.utc),
                    }
                    continue

                api_key = security.decrypt_api_key(key_data["encrypted_key"])
                bars = await torn_api.get_user_bars_v2(api_key)
                cooldowns = await torn_api.get_user_cooldowns_v2(api_key)

                energy = int((((bars or {}).get("bars") or {}).get("energy") or {}).get("current", 0))
                cooldown_data = (cooldowns or {}).get("cooldowns") or {}
                drug_cd = int(cooldown_data.get("drug", 0))
                booster_cd = int(cooldown_data.get("booster", 0))
                ready = energy == 1000 and drug_cd == 0 and booster_cd == 0

                jump_status[discord_id] = {
                    "energy_current": energy,
                    "drug_cd": drug_cd,
                    "booster_cd": booster_cd,
                    "ready_bool": ready,
                    "no_api_key": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            except Exception:
                log.exception("Failed monitoring participant jump_id=%s discord_id=%s", jump_id, discord_id)
                jump_status[discord_id] = {
                    "energy_current": None,
                    "drug_cd": None,
                    "booster_cd": None,
                    "ready_bool": False,
                    "no_api_key": False,
                    "error": True,
                    "updated_at": datetime.now(timezone.utc),
                }

        stale_ids = set(jump_status.keys()) - participant_ids
        for stale_id in stale_ids:
            jump_status.pop(stale_id, None)

        self._needs_refresh.discard(jump_id)
        return True


_jump_monitor: JumpMonitor | None = None


def get_jump_monitor() -> JumpMonitor:
    global _jump_monitor
    if _jump_monitor is None:
        _jump_monitor = JumpMonitor()
    return _jump_monitor
