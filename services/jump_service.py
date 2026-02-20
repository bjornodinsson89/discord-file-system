from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import config
from repositories.audit import AuditRepository
from repositories.jumps import JumpsRepository, SignupStatusSchemaMismatchError
from repositories.users import UsersRepository
from utils.guild_settings_repository import GuildSettingsRepository
from .errors import AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound
from .jump_monitor import get_jump_monitor
from .validation import validate_discord_id, validate_guild_id, validate_positive_int

log = logging.getLogger("happy_jumper.services.jump")


class JumpService:
    def __init__(self, db, torn_api, security_manager):
        self.db = db
        self.torn_api = torn_api
        self.security_manager = security_manager
        self.monitor = get_jump_monitor()
        self.jumps_repo = JumpsRepository(db.pool)
        self.users_repo = UsersRepository(db.pool)
        self.audit_repo = AuditRepository(db.pool)
        self.settings_repo = GuildSettingsRepository(db)

    async def start_monitoring(self, session_id: int) -> None:
        await self.monitor.start(session_id)

    async def stop_monitoring(self, session_id: int) -> None:
        await self.monitor.stop(session_id)

    async def end_jump(self, *, session_id: int, status: str = "completed") -> None:
        validate_positive_int(session_id, field_name="Session ID")
        if status == "completed":
            await self.jumps_repo.complete_session(session_id)
        else:
            await self.jumps_repo.cancel_session(session_id)
        await self.monitor.stop(session_id)

    async def join_session(self, *, session_id: int, guild_id: int, user_id: int) -> dict[str, Any]:
        validate_positive_int(session_id, field_name="Session ID")
        validate_guild_id(guild_id)
        validate_discord_id(user_id)

        try:
            blacklist = await self.jumps_repo.is_blacklisted(guild_id, user_id)
            if blacklist:
                raise BusinessRuleViolation(blacklist.get("reason") or "You are blacklisted")

            session = await self.jumps_repo.get_session(session_id)
            if not session or session.get("status") != "open":
                raise NotFound("Session unavailable")

            key_data = await self.users_repo.get_user_api_key(user_id)
            if not key_data:
                raise InvalidInput("API key required")

            existing = await self.jumps_repo.get_signup(session_id, user_id)
            if existing and existing.get("status") != "cancelled":
                raise AlreadyExists(f"Status: {existing.get('status', 'unknown')}")

            signups = await self.jumps_repo.list_signups(session_id)
            active_count = len([s for s in signups if s.get("status") in {"paid", "completed", "not_completed"}])
            if active_count >= int(session["max_slots"]):
                raise BusinessRuleViolation("Session is full; waitlist is not enabled in this schema")

            api_key = self.security_manager.decrypt(key_data["encrypted_key"])
            user_data = await self.torn_api.get_user_data(api_key)
            drug_cd = int(user_data.get("cooldowns", {}).get("drug", 0) or 0)
            booster_cd = int(user_data.get("cooldowns", {}).get("booster", 0) or 0)
            if drug_cd > 0:
                raise BusinessRuleViolation(f"{drug_cd}s remaining")

            energy = user_data.get("bars", {}).get("energy", {})
            current_energy = energy.get("current", 0)
            max_energy = energy.get("maximum", 150)

            settings = await self.settings_repo.get_or_create(guild_id)
            timeout = settings.get("reservation_timeout_minutes", config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.utcnow() + timedelta(minutes=timeout)

            await self.jumps_repo.create_or_restore_signup(
                session_id=session_id,
                guild_id=guild_id,
                discord_id=user_id,
                torn_user_id=key_data["torn_user_id"],
                reserved_until=reserved_until,
            )
            await self.jumps_repo.upsert_readiness_snapshot(
                session_id=session_id,
                guild_id=guild_id,
                discord_id=user_id,
                energy=current_energy,
                energy_max=max_energy,
                drug_cooldown=drug_cd,
                booster_cooldown=booster_cd,
                status_text="ready",
            )
            await self.audit_repo.log_audit(
                actor_discord_id=user_id,
                action="jump_signup",
                target_type="session",
                target_id=session_id,
                payload={},
                guild_id=guild_id,
                source="services/jump_service.py:join_session",
            )
            self.monitor.mark_needs_refresh(session_id)
            return {"result": "reserved", "reserved_until": reserved_until}
        except SignupStatusSchemaMismatchError as exc:
            log.error("join failed due to DB schema mismatch session_id=%s guild_id=%s user_id=%s", session_id, guild_id, user_id, exc_info=True)
            raise RuntimeError("Join failed due to database schema mismatch. Ask an admin to run migrations.") from exc
        except (AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound):
            raise
        except Exception:
            log.exception("Join session failed session_id=%s user_id=%s", session_id, user_id)
            raise

    async def join_waitlist(self, *, session_id: int, user_id: int) -> int:
        raise BusinessRuleViolation("Waitlist is not enabled in this schema")
