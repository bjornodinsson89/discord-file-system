from __future__ import annotations

import logging
from datetime import datetime, timedelta

import config
from repositories.audit import AuditRepository
from repositories.raffles import RafflesRepository
from repositories.users import UsersRepository
from utils.guild_settings_repository import GuildSettingsRepository
from .errors import AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound
from .validation import validate_discord_id, validate_positive_int

log = logging.getLogger("happy_jumper.services.raffle")


class RaffleService:
    def __init__(self, db):
        self.db = db
        self.raffles_repo = RafflesRepository(db.pool)
        self.users_repo = UsersRepository(db.pool)
        self.audit_repo = AuditRepository(db.pool)
        self.settings_repo = GuildSettingsRepository(db)

    async def reserve_tickets(self, *, raffle_id: int, user_id: int, tickets: int) -> dict:
        validate_positive_int(raffle_id, field_name="Raffle ID")
        validate_discord_id(user_id)
        validate_positive_int(tickets, field_name="Tickets", min_value=1)
        try:
            raffle = await self.raffles_repo.get_raffle(raffle_id)
            if not raffle or raffle.get("status") not in ("active", "open"):
                raise NotFound("Raffle unavailable")

            key_data = await self.users_repo.get_user_api_key(user_id)
            if not key_data:
                raise InvalidInput("API key required")

            entry = await self.raffles_repo.get_entry_by_raffle_and_discord(raffle_id, user_id)
            existing_tickets = int(entry.get("num_tickets", 0)) if entry else 0
            max_per_user = raffle.get("max_tickets_per_user")
            if max_per_user and existing_tickets + tickets > max_per_user:
                raise BusinessRuleViolation(f"Max {max_per_user} tickets per user")

            paid = int(raffle.get("tickets_sold") or 0)
            reserved = await self.raffles_repo.get_reserved_tickets_count(raffle_id)
            used = paid + reserved - existing_tickets
            if int(raffle.get("tickets_available") or 0) == 0:
                available = 10**9
            else:
                available = max(raffle["tickets_available"] - used, 0)
            if tickets > available:
                raise BusinessRuleViolation(f"Only {available} available")

            settings = await self.settings_repo.get_or_create(raffle["guild_id"])
            timeout = settings.get("reservation_timeout_minutes", config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.utcnow() + timedelta(minutes=timeout)

            saved = await self.raffles_repo.reserve_entry(raffle_id, user_id, key_data["torn_user_id"], tickets, reserved_until)
            return {"raffle": raffle, "entry": saved, "reserved_until": reserved_until}
        except (NotFound, InvalidInput, BusinessRuleViolation, AlreadyExists):
            raise
        except Exception:
            log.exception("Reserve raffle tickets failed raffle_id=%s user_id=%s", raffle_id, user_id)
            raise

    async def draw_raffle(self, *, raffle_id: int, actor_discord_id: int, source: str = "discord") -> dict:
        validate_positive_int(raffle_id, field_name="Raffle ID")
        validate_discord_id(actor_discord_id)
        raffle = await self.raffles_repo.get_raffle(raffle_id)
        if not raffle:
            raise NotFound("Raffle not found")
        if raffle["status"] != "active":
            raise BusinessRuleViolation("Raffle is not active")
        winner = await self.raffles_repo.draw_winner(raffle_id)
        await self.audit_repo.log_audit(
            actor_discord_id=actor_discord_id,
            action="raffle_drawn",
            target_type="raffle",
            target_id=raffle_id,
            payload={"winner_discord_id": winner["discord_id"] if winner else None},
            guild_id=raffle["guild_id"],
            source=source,
        )
        return {"raffle": raffle, "winner": winner}
