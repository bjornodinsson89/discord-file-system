from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from repositories.raffles import RafflesRepository
from utils import get_security_manager, get_torn_api
from utils.torn_api import TornAPIError

log = logging.getLogger("happy_jumper.services.raffle_payment")


class RafflePaymentService:
    def __init__(self, db):
        self.db = db
        self.repo = RafflesRepository(db.pool)

    async def verify_raffle_payment(self, entry_id: int, manual: bool) -> tuple[bool, Optional[int], Optional[str]]:
        entry = await self.repo.get_entry_for_verification(entry_id)
        if not entry:
            return False, None, "Entry not found"

        if entry.get("payment_verified"):
            return True, None, None

        if entry.get("ticket_payment_type") == "free":
            verified = await self.repo.mark_entry_verified(entry_id)
            if not verified:
                return False, None, "Entry not found"
            sold_out_id = await self.repo.recompute_tickets_sold_and_set_sold_out(int(entry["raffle_id"]))
            return True, sold_out_id, None

        reserved_until = entry.get("reserved_until")
        if not manual and reserved_until and reserved_until < datetime.utcnow():
            return False, None, "Reservation expired"

        buyer_key = await self.db.get_user_api_key(int(entry["discord_id"]))
        if not buyer_key or not buyer_key.get("encrypted_key"):
            return False, None, "❌ You must link your Torn API key first to verify paid raffle tickets."

        buyer_torn_id = entry.get("torn_user_id")
        creator_torn_id = entry.get("effective_creator_torn_id")

        if not buyer_torn_id:
            return False, None, "Buyer Torn ID missing for this reservation. Please reserve again."
        if not creator_torn_id:
            return False, None, "Raffle creator Torn ID is missing. Ask an admin to fix creator API key linkage."

        security = get_security_manager()
        torn_api = get_torn_api()

        try:
            api_key = security.decrypt(buyer_key["encrypted_key"])
            logs = await torn_api.get_user_logs(api_key, limit=200)
        except TornAPIError:
            return False, None, "Verification temporarily unavailable. Try again."
        except Exception:
            log.exception("Unexpected raffle verification error entry_id=%s", entry_id)
            return False, None, "Verification temporarily unavailable. Try again."

        expected_qty = int(entry["ticket_price"]) * int(entry["num_tickets"])
        item_type = str(entry["ticket_payment_type"]).lower()

        created_at = entry.get("created_at")
        since_ts = int(created_at.replace(tzinfo=timezone.utc).timestamp()) if created_at else 0
        until_ts = None
        if reserved_until:
            until_ts = int((reserved_until + timedelta(minutes=2)).replace(tzinfo=timezone.utc).timestamp())

        match = self._find_matching_payment(
            logs=logs,
            creator_torn_id=int(creator_torn_id),
            item_type=item_type,
            required_qty=expected_qty,
            since_ts=since_ts,
            until_ts=until_ts,
        )

        if not match:
            return False, None, "Payment not found yet. Make sure you sent the items to the creator."

        verified = await self.repo.mark_entry_verified(entry_id)
        if not verified:
            return False, None, "Entry not found"

        sold_out_id = await self.repo.recompute_tickets_sold_and_set_sold_out(int(entry["raffle_id"]))
        return True, sold_out_id, None

    def _find_matching_payment(
        self,
        *,
        logs: list[dict[str, Any]],
        creator_torn_id: int,
        item_type: str,
        required_qty: int,
        since_ts: int,
        until_ts: Optional[int],
    ) -> Optional[dict[str, Any]]:
        aliases = ["xanax"] if item_type == "xanax" else ["erotic dvd", "edvd", "erotic_dvd", "dvd"]

        for entry in logs:
            ts = self._extract_timestamp(entry)
            if ts is None:
                continue
            if ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue

            if not self._mentions_recipient(entry, creator_torn_id):
                continue
            if not self._looks_like_transfer(entry):
                continue

            qty = self._extract_item_quantity(entry, aliases)
            if qty >= required_qty:
                return entry

        return None

    @staticmethod
    def _extract_timestamp(entry: dict[str, Any]) -> Optional[int]:
        for key in ("timestamp", "time", "created", "created_at"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    @staticmethod
    def _looks_like_transfer(entry: dict[str, Any]) -> bool:
        payload = json.dumps(entry, ensure_ascii=False).lower()
        return any(token in payload for token in ("sent", "send", "transfer", "item", "trade", "gave"))

    @staticmethod
    def _mentions_recipient(entry: dict[str, Any], creator_torn_id: int) -> bool:
        target = str(creator_torn_id)
        payload = json.dumps(entry, ensure_ascii=False).lower()
        if target in payload:
            return True
        return False

    @staticmethod
    def _extract_item_quantity(entry: dict[str, Any], aliases: list[str]) -> int:
        payload = json.dumps(entry, ensure_ascii=False).lower()
        qty = 0

        for alias in aliases:
            for pattern in (
                rf"(\d+)\s*x\s*{re.escape(alias)}",
                rf"(\d+)\s*{re.escape(alias)}",
                rf"{re.escape(alias)}\s*x\s*(\d+)",
                rf"{re.escape(alias)}[^\d]{{0,16}}(\d+)",
            ):
                for match in re.findall(pattern, payload):
                    try:
                        qty = max(qty, int(match))
                    except (TypeError, ValueError):
                        continue

        return qty
