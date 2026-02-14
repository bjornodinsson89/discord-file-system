from __future__ import annotations

from typing import Any, Optional


class PaymentReceiptService:
    def __init__(self, pool):
        self.pool = pool

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
        async with self.pool.acquire() as conn:
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
                RETURNING id
                """,
                featureType,
                str(featureRefId),
                payer_discord_id,
                payer_torn_id,
                amount,
                currency_type,
                receipt_hash or f"auto:{featureType}:{featureRefId}:{payer_discord_id}:{amount}",
                metadata or {},
            )
            return int(row["id"])

    async def markVerified(self, *, receiptId: int, verifier_discord_id: Optional[int], verifier_torn_id: Optional[int] = None, verification_metadata: Optional[dict[str, Any]] = None) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE payment_receipts
                SET status = 'verified',
                    verified_at = NOW(),
                    verified_by_discord_id = $2,
                    receipt_meta = COALESCE(receipt_meta, '{}'::jsonb) || $3::jsonb
                WHERE id = $1
                RETURNING id
                """,
                receiptId,
                verifier_discord_id,
                verification_metadata or {},
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
        async with self.pool.acquire() as conn:
            async with conn.transaction():
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
                    RETURNING id
                    """,
                    featureType,
                    str(featureRefId),
                    payer_discord_id,
                    payer_torn_id,
                    amount,
                    currency_type,
                    receipt_hash or f"auto:{featureType}:{featureRefId}:{payer_discord_id}:{amount}",
                    metadata or {},
                )
                receipt_id = int(row["id"])
                await conn.execute(
                    """
                    UPDATE payment_receipts
                    SET status = 'verified',
                        verified_at = NOW(),
                        verified_by_discord_id = $2,
                        receipt_meta = COALESCE(receipt_meta, '{}'::jsonb) || $3::jsonb
                    WHERE id = $1
                    """,
                    receipt_id,
                    verifier_discord_id,
                    {"verifier_torn_id": verifier_torn_id, "payee_discord_id": payee_discord_id, "payee_torn_id": payee_torn_id},
                )
                return receipt_id
    async def markRejected(self, *, receiptId: int, verifier_discord_id: Optional[int], verification_metadata: Optional[dict[str, Any]] = None) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE payment_receipts
                SET status = 'rejected',
                    verified_at = NOW(),
                    verified_by_discord_id = $2,
                    receipt_meta = COALESCE(receipt_meta, '{}'::jsonb) || $3::jsonb
                WHERE id = $1
                RETURNING id
                """,
                receiptId,
                verifier_discord_id,
                verification_metadata or {},
            )
            return row is not None
