from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from repositories.raffles import RafflesRepository
from repositories.users import UsersRepository
from utils import get_security_manager, get_torn_api
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError
from services.payment_receipts import PaymentReceiptService
from utils.payment_normalization import parse_payment_type

log = logging.getLogger("happy_jumper.services.raffle_payment")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RafflePaymentService:
    def __init__(self, db):
        self.db = db
        self.repo = RafflesRepository(db.pool)
        self.users_repo = UsersRepository(db.pool)

    async def verify_entry_payment(self, entry_id: int, manual: bool = False) -> tuple[bool, Optional[int], Optional[str]]:
        reserved_until = None
        created_at = None

        try:
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
            now = utcnow()
            if reserved_until and reserved_until.tzinfo is None:
                reserved_until = reserved_until.replace(tzinfo=timezone.utc)

            if not manual and reserved_until and reserved_until < now:
                return False, None, "Reservation expired"

            buyer_key = await self.users_repo.get_user_api_key(int(entry["discord_id"]))
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
                logs = await torn_api.get_item_send_receive_logs(
                    api_key,
                    limit=config.PAYMENT_VERIFICATION_LOG_LIMIT,
                    audit_discord_id=int(entry["discord_id"]),
                    audit_torn_id=buyer_torn_id,
                    audit_context="payment_verify_logs",
                    audit_query_meta={"cat": 85, "limit": int(config.PAYMENT_VERIFICATION_LOG_LIMIT)},
                )
            except TornAPIRateLimitError:
                return False, None, "Torn API is rate-limited right now. Please try again in a moment."
            except TornAPIPermissionError:
                return False, None, "Your Torn API key lacks permission to read item logs (cat=85). Update key permissions and try again."
            except TornAPIError:
                return False, None, "Torn verification is temporarily unavailable. Please try again shortly."
            except Exception:
                reserved_until_tz = (
                    "naive" if reserved_until and reserved_until.tzinfo is None else "aware" if reserved_until else "missing"
                )
                created_at_tz = "naive" if created_at and created_at.tzinfo is None else "aware" if created_at else "missing"
                log.exception(
                    "Unexpected raffle verification error entry_id=%s manual=%s reserved_until_tz=%s created_at_tz=%s",
                    entry_id,
                    manual,
                    reserved_until_tz,
                    created_at_tz,
                )
                return False, None, "Torn verification is temporarily unavailable. Please try again shortly."

            expected_qty = int(entry["ticket_price"] or 0) * int(entry["num_tickets"] or 0)
            try:
                item_type = parse_payment_type(str(entry["ticket_payment_type"]), allow_free=False)
            except ValueError:
                item_type = ""

            if expected_qty <= 0:
                return False, None, "Invalid paid ticket quantity for verification."

            required_item_id = 206 if item_type == "xanax" else 366 if item_type == "erotic_dvd" else 0

            if not required_item_id:
                return False, None, "Invalid payment type configured for this raffle."

            created_at = entry.get("created_at")
            if created_at:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                since_dt = created_at - timedelta(minutes=3)
            else:
                since_dt = utcnow() - timedelta(minutes=10)
            since_ts = int(since_dt.timestamp())

            # Soft window to avoid matching historical sends.
            until_ts = None
            if reserved_until:
                if reserved_until.tzinfo is None:
                    reserved_until = reserved_until.replace(tzinfo=timezone.utc)
                until_dt = reserved_until + timedelta(minutes=2)
                until_ts = int(until_dt.timestamp())

            match = self._find_matching_payment(
                logs=logs,
                sender_torn_id=buyer_torn_id,
                creator_torn_id=creator_torn_id,
                required_item_id=required_item_id,
                required_qty=expected_qty,
                since_ts=since_ts,
                until_ts=until_ts,
            )
            if not match:
                return False, None, "Payment not found in Torn logs yet. Make sure items were sent to the raffle creator."

            verified = await self.repo.mark_entry_verified(entry_id)
            if not verified:
                return False, None, "Entry not found"

            receipts = PaymentReceiptService(self.db.pool)
            await receipts.create_and_verify(
                featureType="raffle",
                featureRefId=int(entry_id),
                payer_discord_id=int(entry.get("discord_id") or 0) or None,
                payer_torn_id=buyer_torn_id,
                payee_discord_id=int(entry.get("creator_discord_id") or 0) or None,
                payee_torn_id=creator_torn_id,
                amount=expected_qty,
                currency_type=item_type,
                metadata=match,
                verifier_discord_id=int(entry.get("discord_id") or 0) or None,
                verifier_torn_id=buyer_torn_id,
            )

            sold_out_id = await self.repo.recompute_tickets_sold_and_maybe_set_sold_out(int(entry["raffle_id"]))
            return True, sold_out_id, None
        except Exception:
            reserved_until_tz = (
                "naive" if reserved_until and reserved_until.tzinfo is None else "aware" if reserved_until else "missing"
            )
            created_at_tz = "naive" if created_at and created_at.tzinfo is None else "aware" if created_at else "missing"
            log.exception(
                "Unhandled raffle verify error entry_id=%s manual=%s reserved_until_tz=%s created_at_tz=%s",
                entry_id,
                manual,
                reserved_until_tz,
                created_at_tz,
            )
            return False, None, "Torn verification is temporarily unavailable. Please try again shortly."

    async def verify_raffle_payment(self, entry_id: int, manual: bool) -> tuple[bool, Optional[int], Optional[str]]:
        """Backward-compatible alias."""
        return await self.verify_entry_payment(entry_id, manual=manual)

    def _find_matching_payment(
        self,
        *,
        logs: list[dict[str, Any]],
        sender_torn_id: int,
        creator_torn_id: int,
        required_item_id: int,
        required_qty: int,
        since_ts: int,
        until_ts: Optional[int],
    ) -> Optional[dict[str, Any]]:
        _ = sender_torn_id

        for entry in logs:
            raw_ts = entry.get("timestamp")
            try:
                ts = int(raw_ts or 0)
            except (TypeError, ValueError):
                continue
            if ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue

            data = entry.get("data") or {}
            if int(data.get("receiver") or 0) != int(creator_torn_id):
                continue

            qty_sent = sum(
                int(item.get("qty") or 0)
                for item in (data.get("items") or [])
                if int(item.get("id") or 0) == required_item_id
            )
            if qty_sent >= required_qty:
                return entry

        return None
