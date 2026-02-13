from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import config
from .errors import AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound
from .jump_monitor import get_jump_monitor
from repositories.jumps import JumpsRepository
from .validation import validate_discord_id, validate_guild_id, validate_positive_int

log = logging.getLogger("happy_jumper.services.jump")


class JumpService:
    def __init__(self, db, torn_api, security_manager):
        self.db = db
        self.torn_api = torn_api
        self.security_manager = security_manager
        self.monitor = get_jump_monitor()

    async def start_monitoring(self, session_id: int) -> None:
        await self.monitor.start(session_id)

    async def stop_monitoring(self, session_id: int) -> None:
        await self.monitor.stop(session_id)

    async def end_jump(self, *, session_id: int, status: str = "completed") -> None:
        validate_positive_int(session_id, field_name="Session ID")
        repo = JumpsRepository(self.db.pool)
        if status == "completed":
            await repo.complete_session(session_id)
        else:
            await repo.cancel_session(session_id)
        await self.monitor.stop(session_id)

    async def join_session(self, *, session_id: int, guild_id: int, user_id: int) -> dict[str, Any]:
        validate_positive_int(session_id, field_name="Session ID")
        validate_guild_id(guild_id)
        validate_discord_id(user_id)

        try:
            blacklist = await self.db.is_blacklisted(guild_id, user_id)
            if blacklist:
                raise BusinessRuleViolation(blacklist.get("reason") or "You are blacklisted")

            session = await self.db.get_jump_session(session_id)
            if not session or session.get("status") != "open":
                raise NotFound("Session unavailable")

            key_data = await self.db.get_user_api_key(user_id)
            if not key_data:
                raise InvalidInput("API key required")

            existing = await self.db.get_signup(session_id, user_id)
            if existing:
                raise AlreadyExists(f"Status: {existing.get('status', 'unknown')}")

            signups = await self.db.get_session_signups(session_id)
            if len(signups) >= session["max_spots"]:
                position = await self.db.add_to_waitlist(session_id, user_id, key_data["torn_user_id"])
                return {"result": "waitlist", "position": position}

            api_key = self.security_manager.decrypt(key_data["encrypted_key"])
            user_data = await self.torn_api.get_user_data(api_key)
            drug_cd = user_data.get("cooldowns", {}).get("drug", 0)
            if drug_cd > 0:
                raise BusinessRuleViolation(f"{drug_cd}s remaining")

            energy = user_data.get("bars", {}).get("energy", {})
            current_energy = energy.get("current", 0)
            max_energy = energy.get("maximum", 150)

            settings = await self.db.get_guild_settings(guild_id)
            timeout = settings.get("reservation_timeout_minutes", config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.utcnow() + timedelta(minutes=timeout)

            await self.db.create_signup(session_id, user_id, key_data["torn_user_id"], reserved_until)
            await self.db.update_readiness(session_id, user_id, current_energy, max_energy, drug_cd, "ready")
            await self.db.log_audit(user_id, "jump_signup", "session", session_id)
            self.monitor.mark_needs_refresh(session_id)
            return {"result": "reserved", "reserved_until": reserved_until}
        except (AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound):
            raise
        except Exception:
            log.exception("Join session failed session_id=%s user_id=%s", session_id, user_id)
            raise

    async def join_waitlist(self, *, session_id: int, user_id: int) -> int:
        validate_positive_int(session_id, field_name="Session ID")
        validate_discord_id(user_id)
        try:
            key_data = await self.db.get_user_api_key(user_id)
            if not key_data:
                raise InvalidInput("API key required")
            existing = await self.db.get_signup(session_id, user_id)
            if existing:
                raise AlreadyExists("Already signed up")
            waitlist = await self.db.get_session_waitlist(session_id)
            if any(w["discord_id"] == user_id for w in waitlist):
                raise AlreadyExists("Already on waitlist")
            return await self.db.add_to_waitlist(session_id, user_id, key_data["torn_user_id"])
        except (AlreadyExists, InvalidInput):
            raise
        except Exception:
            log.exception("Join waitlist failed session_id=%s user_id=%s", session_id, user_id)
            raise
