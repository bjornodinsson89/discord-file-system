from __future__ import annotations

from typing import Any, Optional


class PaymentReceiptService:
    def __init__(self, pool):
        self.pool = pool

    async def createReceipt(
        self,
        *,
        featureType: str,
        featureRefId: int,
        payer_discord_id: Optional[int],
        payer_torn_id: Optional[int],
        payee_discord_id: Optional[int],
        payee_torn_id: Optional[int],
        amount: int,
        currency_type: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO payment_receipts (
                    feature_type,
                    feature_ref_id,
                    payer_discord_id,
                    payer_torn_id,
                    payee_discord_id,
                    payee_torn_id,
                    amount,
                    currency_type,
                    metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                RETURNING id
                """,
                featureType,
                featureRefId,
                payer_discord_id,
                payer_torn_id,
                payee_discord_id,
                payee_torn_id,
                amount,
                currency_type,
                metadata or {},
            )
            return int(row["id"])

    async def markVerified(
        self,
        *,
        receiptId: int,
        verifier_discord_id: Optional[int],
        verifier_torn_id: Optional[int],
        verification_metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE payment_receipts
                SET verified = TRUE,
                    verified_at = NOW(),
                    verifier_discord_id = $2,
                    verifier_torn_id = $3,
                    verification_metadata = $4::jsonb,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING id
                """,
                receiptId,
                verifier_discord_id,
                verifier_torn_id,
                verification_metadata or {},
            )
            return row is not None
