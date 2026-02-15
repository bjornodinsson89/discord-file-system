from __future__ import annotations

import logging
from typing import Any

from repositories.jumps import JumpsRepository
from repositories.overdose import OverdoseRepository
from repositories.users import UsersRepository
from utils import get_security_manager, get_torn_api


log = logging.getLogger("happy_jumper.overdose_tracker")


class OverdoseTrackerError(Exception):
    """Controlled overdose tracking failure."""


class OverdoseTracker:
    def __init__(self, *, users_repo: UsersRepository, overdose_repo: OverdoseRepository, jumps_repo: JumpsRepository | None = None):
        self.users_repo = users_repo
        self.overdose_repo = overdose_repo
        self.jumps_repo = jumps_repo

    async def check_user_since(
        self,
        *,
        guild_id: int,
        discord_id: int,
        since_ts: int,
        session_id: int | None = None,
    ) -> dict[str, Any] | None:
        key_row = await self.users_repo.get_user_api_key(discord_id)
        encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
        if not encrypted_key:
            return None

        try:
            api_key = get_security_manager().decrypt_api_key(encrypted_key)
            logs = await get_torn_api().check_drug_use_logs(api_key, since_timestamp=since_ts)
        except Exception as exc:
            raise OverdoseTrackerError(f"Torn API failed for discord_id={discord_id}: {exc}") from exc

        for entry in logs:
            od = await get_torn_api().identify_overdose_event(entry)
            if not od:
                continue

            event_timestamp = int((entry or {}).get("timestamp") or od.get("timestamp") or 0)
            if event_timestamp < int(since_ts):
                continue

            event_type_raw = str(od.get("type") or "").strip().lower()
            event_type = "ecstasy_overdose" if "ecstasy" in event_type_raw else "xanax_overdose"
            torn_log_id = str(od.get("log_id") or entry.get("id") or entry.get("log_id") or entry.get("log") or "")
            if not torn_log_id:
                continue

            meta = {
                "normalized": {
                    "event_type": event_type,
                    "event_timestamp": event_timestamp,
                    "torn_log_id": torn_log_id,
                },
                "raw": od,
            }
            inserted = await self.overdose_repo.insert_event_if_new(
                guild_id=guild_id,
                discord_id=discord_id,
                torn_user_id=(key_row or {}).get("torn_user_id"),
                event_type=event_type,
                event_timestamp=event_timestamp,
                torn_log_id=torn_log_id,
                meta=meta,
            )

            session_marked = False
            if session_id is not None and self.jumps_repo is not None:
                session_marked = await self.jumps_repo.mark_signup_overdose(
                    session_id=session_id,
                    guild_id=guild_id,
                    discord_id=discord_id,
                    torn_log_id=torn_log_id,
                    event_timestamp=event_timestamp,
                    meta_json=meta,
                )

            if inserted or session_marked:
                return {
                    "event_type": event_type,
                    "event_timestamp": event_timestamp,
                    "torn_log_id": torn_log_id,
                    "raw": od,
                    "session_marked": session_marked,
                }

        return None
