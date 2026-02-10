from __future__ import annotations

import logging
from datetime import datetime, timedelta

import config
from .errors import AlreadyExists, BusinessRuleViolation, InvalidInput, NotFound
from .validation import validate_discord_id, validate_positive_int

log = logging.getLogger("happy_jumper.services.raffle")


class RaffleService:
    def __init__(self, db):
        self.db = db

    async def reserve_tickets(self, *, raffle_id: int, user_id: int, tickets: int) -> dict:
        validate_positive_int(raffle_id, field_name="Raffle ID")
        validate_discord_id(user_id)
        validate_positive_int(tickets, field_name="Tickets", min_value=1)
        try:
            raffle = await self.db.get_raffle(raffle_id)
            if not raffle or raffle.get("status") not in ("active", "open"):
                raise NotFound("Raffle unavailable")

            key_data = await self.db.get_user_api_key(user_id)
            if not key_data:
                raise InvalidInput("API key required")

            entry = await self.db.get_raffle_entry(raffle_id, user_id)
            existing_tickets = int(entry.get("num_tickets", 0)) if entry else 0
            max_per_user = raffle.get("max_tickets_per_user")
            if max_per_user and existing_tickets + tickets > max_per_user:
                raise BusinessRuleViolation(f"Max {max_per_user} tickets per user")

            paid = await self.db.get_raffle_ticket_count(raffle_id)
            reserved = await self.db.get_raffle_reserved_ticket_count(raffle_id)
            used = paid + reserved - existing_tickets
            available = max(raffle["tickets_available"] - used, 0)
            if tickets > available:
                raise BusinessRuleViolation(f"Only {available} available")

            settings = await self.db.get_guild_settings(raffle["guild_id"])
            timeout = settings.get("reservation_timeout_minutes", config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.utcnow() + timedelta(minutes=timeout)

            saved = await self.db.create_raffle_entry(raffle_id, user_id, key_data["torn_user_id"], tickets, reserved_until)
            return {"raffle": raffle, "entry": saved, "reserved_until": reserved_until}
        except (NotFound, InvalidInput, BusinessRuleViolation, AlreadyExists):
            raise
        except Exception:
            log.exception("Reserve raffle tickets failed raffle_id=%s user_id=%s", raffle_id, user_id)
            raise

    async def draw_raffle(self, *, raffle_id: int, actor_discord_id: int, source: str = "discord") -> dict:
        validate_positive_int(raffle_id, field_name="Raffle ID")
        validate_discord_id(actor_discord_id)
        raffle = await self.db.get_raffle(raffle_id)
        if not raffle:
            raise NotFound("Raffle not found")
        if raffle["status"] != "active":
            raise BusinessRuleViolation("Raffle is not active")
        winner = await self.db.draw_raffle_winner(raffle_id)
        await self.db.log_audit(
            actor_discord_id,
            "raffle_drawn",
            "raffle",
            raffle_id,
            {"winner_discord_id": winner["discord_id"] if winner else None},
            guild_id=raffle["guild_id"],
            source=source,
        )
        return {"raffle": raffle, "winner": winner}
