from __future__ import annotations

from typing import Any, Optional

import config
from utils.db_acquire import acquire_conn
from utils.json_safe import json_dumps_safe


class PaymentReceiptService:
    def __init__(self, pool):
        self.pool = pool

    @staticmethod
    def _compact_json(payload: Optional[dict[str, Any]]) -> str:
        return json_dumps_safe(payload or {}, sort_keys=True)

    @staticmethod
    def _default_hash(
        *,
        feature_type: str,
        feature_ref_id: int | str,
        payer_discord_id: Optional[int],
        amount: int,
    ) -> str:
        return f"auto:{feature_type}:{feature_ref_id}:{payer_discord_id}:{amount}"

    async def createReceipt(
        self,
        *,
        featureType: str,
        featureRefId: int | str,
        payer_discord_id: Optional[int],
        payer_torn_id: Optional[int],
        payee_discord_id: Optional[int] = None,
        payee_torn_id: Optional[int] = None,
        amount: int,
        currency_type: str,
        metadata: Optional[dict[str, Any]] = None,
        receipt_hash: Optional[str] = None,
    ) -> int:
        resolved_hash = receipt_hash or self._default_hash(
            feature_type=featureType,
            feature_ref_id=featureRefId,
            payer_discord_id=payer_discord_id,
            amount=amount,
        )
        async with acquire_conn(self.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO payment_receipts (
                    feature_type,
                    feature_ref_id,
                    payer_discord_id,
                    payer_torn_user_id,
                    amount,
                    currency,
                    receipt_hash,
                    receipt_meta,
                    status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,'pending')
                ON CONFLICT (receipt_hash) DO UPDATE
                SET receipt_meta = COALESCE(payment_receipts.receipt_meta, '{}'::jsonb) || EXCLUDED.receipt_meta,
                    updated_at = NOW()
                RETURNING id
                """,
                featureType,
                str(featureRefId),
                payer_discord_id,
                payer_torn_id,
                amount,
                currency_type,
                resolved_hash,
                self._compact_json(metadata),
            )
            return int(row["id"])

    async def markVerified(
        self,
        *,
        receiptId: int,
        verifier_discord_id: Optional[int],
        verifier_torn_id: Optional[int] = None,
        verification_metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        merged_meta = dict(verification_metadata or {})
        if verifier_torn_id is not None and "verifier_torn_id" not in merged_meta:
            merged_meta["verifier_torn_id"] = verifier_torn_id
        async with acquire_conn(self.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                UPDATE payment_receipts
                SET status = 'verified',
                    verified_at = COALESCE(verified_at, NOW()),
                    verified_by_discord_id = COALESCE(verified_by_discord_id, $2),
                    receipt_meta = COALESCE(receipt_meta, '{}'::jsonb) || $3::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                receiptId,
                verifier_discord_id,
                self._compact_json(merged_meta),
            )
            return row is not None

    async def create_and_verify(
        self,
        *,
        featureType: str,
        featureRefId: int | str,
        payer_discord_id: Optional[int],
        payer_torn_id: Optional[int],
        payee_discord_id: Optional[int] = None,
        payee_torn_id: Optional[int] = None,
        amount: int,
        currency_type: str,
        metadata: Optional[dict[str, Any]] = None,
        verifier_discord_id: Optional[int] = None,
        verifier_torn_id: Optional[int] = None,
        receipt_hash: Optional[str] = None,
    ) -> int:
        resolved_hash = receipt_hash or self._default_hash(
            feature_type=featureType,
            feature_ref_id=featureRefId,
            payer_discord_id=payer_discord_id,
            amount=amount,
        )
        verify_meta = {
            "verifier_torn_id": verifier_torn_id,
            "payee_discord_id": payee_discord_id,
            "payee_torn_id": payee_torn_id,
        }
        async with acquire_conn(self.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO payment_receipts (
                    feature_type,
                    feature_ref_id,
                    payer_discord_id,
                    payer_torn_user_id,
                    amount,
                    currency,
                    receipt_hash,
                    receipt_meta,
                    status,
                    verified_at,
                    verified_by_discord_id
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,($8::jsonb || $9::jsonb),'verified',NOW(),$10)
                ON CONFLICT (receipt_hash) DO UPDATE
                SET status = 'verified',
                    verified_at = COALESCE(payment_receipts.verified_at, NOW()),
                    verified_by_discord_id = COALESCE(payment_receipts.verified_by_discord_id, EXCLUDED.verified_by_discord_id),
                    receipt_meta = COALESCE(payment_receipts.receipt_meta, '{}'::jsonb) || EXCLUDED.receipt_meta,
                    updated_at = NOW()
                RETURNING id
                """,
                featureType,
                str(featureRefId),
                payer_discord_id,
                payer_torn_id,
                amount,
                currency_type,
                resolved_hash,
                self._compact_json(metadata),
                self._compact_json(verify_meta),
                verifier_discord_id,
            )
            return int(row["id"])

    async def markRejected(
        self,
        *,
        receiptId: int,
        verifier_discord_id: Optional[int],
        verification_metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        async with acquire_conn(self.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
            row = await conn.fetchrow(
                """
                UPDATE payment_receipts
                SET status = 'rejected',
                    verified_at = COALESCE(verified_at, NOW()),
                    verified_by_discord_id = COALESCE(verified_by_discord_id, $2),
                    receipt_meta = COALESCE(receipt_meta, '{}'::jsonb) || $3::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                receiptId,
                verifier_discord_id,
                self._compact_json(verification_metadata),
            )
            return row is not None
