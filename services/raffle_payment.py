from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from repositories.raffles import RafflesRepository
from utils import get_security_manager, get_torn_api
from utils.torn_api import TornAPIError, TornAPIRateLimitError

log = logging.getLogger("happy_jumper.services.raffle_payment")


class RafflePaymentService:
    def __init__(self, db):
        self.db = db
        self.repo = RafflesRepository(db.pool)

    async def verify_entry_payment(self, entry_id: int, manual: bool = False) -> tuple[bool, Optional[int], Optional[str]]:
        entry = await self.repo.get_entry_with_raffle(entry_id)
        if not entry:
            return False, None, "Entry not found"

        if entry.get("payment_verified"):
            return True, None, None

        if str(entry.get("status")) != "active":
            return False, None, "Raffle is no longer active."

        if entry.get("ticket_payment_type") == "free":
            verified = await self.repo.mark_entry_verified(entry_id)
            if not verified:
                return False, None, "Entry not found"
            sold_out_id = await self.repo.recompute_tickets_sold_and_maybe_set_sold_out(int(entry["raffle_id"]))
            return True, sold_out_id, None

        reserved_until = entry.get("reserved_until")
        if not manual and reserved_until and reserved_until < datetime.utcnow():
            return False, None, "Reservation expired"

        buyer_key = await self.db.get_user_api_key(int(entry["discord_id"]))
        if not buyer_key or not buyer_key.get("encrypted_key"):
            return False, None, "You must link your Torn API key first to verify paid raffle tickets."

        buyer_torn_id = int(entry.get("torn_user_id") or 0)
        creator_torn_id = int(entry.get("effective_creator_torn_id") or 0)
        if not buyer_torn_id:
            return False, None, "Buyer Torn ID missing for this reservation. Please reserve again."
        if not creator_torn_id:
            return False, None, "Raffle creator Torn ID is missing. Ask an admin to fix creator API key linkage."

        linked_torn_id = int(buyer_key.get("torn_user_id") or 0)
        if not linked_torn_id or linked_torn_id != buyer_torn_id:
            return False, None, "Linked Torn account does not match this reservation. Reserve tickets again with your current API key."

        security = get_security_manager()
        torn_api = get_torn_api()

        try:
            api_key = security.decrypt(buyer_key["encrypted_key"])
            logs = await torn_api.get_user_logs(api_key, limit=200)
        except TornAPIRateLimitError:
            return False, None, "Torn API is rate-limited right now. Please try again in a moment."
        except TornAPIError:
            return False, None, "Torn verification is temporarily unavailable. Please try again shortly."
        except Exception:
            log.exception("Unexpected raffle verification error entry_id=%s", entry_id)
            return False, None, "Torn verification is temporarily unavailable. Please try again shortly."

        expected_qty = int(entry["ticket_price"] or 0) * int(entry["num_tickets"] or 0)
        item_type = str(entry["ticket_payment_type"]).lower()
        if expected_qty <= 0:
            return False, None, "Invalid paid ticket quantity for verification."

        created_at = entry.get("created_at")
        since_dt = (created_at - timedelta(seconds=30)) if created_at else datetime.utcnow() - timedelta(minutes=10)
        since_ts = int(since_dt.replace(tzinfo=timezone.utc).timestamp())

        # Soft window to avoid matching historical sends.
        until_ts = None
        if reserved_until:
            until_ts = int((reserved_until + timedelta(minutes=2)).replace(tzinfo=timezone.utc).timestamp())

        match = self._find_matching_payment(
            logs=logs,
            sender_torn_id=buyer_torn_id,
            creator_torn_id=creator_torn_id,
            item_type=item_type,
            required_qty=expected_qty,
            since_ts=since_ts,
            until_ts=until_ts,
        )
        if not match:
            return False, None, "Payment not found in Torn logs yet. Make sure items were sent to the raffle creator."

        verified = await self.repo.mark_entry_verified(entry_id)
        if not verified:
            return False, None, "Entry not found"

        sold_out_id = await self.repo.recompute_tickets_sold_and_maybe_set_sold_out(int(entry["raffle_id"]))
        return True, sold_out_id, None

    async def verify_raffle_payment(self, entry_id: int, manual: bool) -> tuple[bool, Optional[int], Optional[str]]:
        """Backward-compatible alias."""
        return await self.verify_entry_payment(entry_id, manual=manual)

    def _find_matching_payment(
        self,
        *,
        logs: list[dict[str, Any]],
        sender_torn_id: int,
        creator_torn_id: int,
        item_type: str,
        required_qty: int,
        since_ts: int,
        until_ts: Optional[int],
    ) -> Optional[dict[str, Any]]:
        aliases = ["xanax", str(config.XANAX_ITEM_ID)] if item_type == "xanax" else ["erotic dvd", "edvd", "erotic_dvd", "dvd", str(config.DVD_ITEM_ID)]

        for entry in logs:
            ts = self._extract_timestamp(entry)
            if ts is None or ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue

            if not self._mentions_sender(entry, sender_torn_id):
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
    def _mentions_sender(entry: dict[str, Any], sender_torn_id: int) -> bool:
        payload = json.dumps(entry, ensure_ascii=False).lower()
        sender = str(sender_torn_id)
        keys = ("sender", "from", "user", "player", "owner")
        # The logs are read from the buyer's own key; allow this as primary source of sender identity.
        if sender in payload:
            return True
        for key in keys:
            value = entry.get(key)
            if isinstance(value, (int, str)) and sender == str(value):
                return True
        return False

    @staticmethod
    def _mentions_recipient(entry: dict[str, Any], creator_torn_id: int) -> bool:
        target = str(creator_torn_id)
        payload = json.dumps(entry, ensure_ascii=False).lower()
        if target in payload:
            return True
        for key in ("receiver", "recipient", "to", "target"):
            value = entry.get(key)
            if isinstance(value, (int, str)) and target == str(value):
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

        for key in ("quantity", "qty", "amount"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                qty = max(qty, int(value))

        return qty
