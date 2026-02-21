from __future__ import annotations

from datetime import datetime
from typing import Optional

from .base import RepositoryBase


class HostTaxRepository(RepositoryBase):
    async def get_recent_receipt(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        recipient_torn_id: int,
        tax_type: str,
        item_id: Optional[int],
        quantity: Optional[int],
        cash_amount: Optional[int],
        since_dt: datetime,
    ) -> Optional[dict]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM host_tax_receipts
                WHERE guild_id = $1
                  AND discord_user_id = $2
                  AND recipient_torn_id = $3
                  AND tax_type = $4
                  AND COALESCE(item_id, -1) = COALESCE($5, -1)
                  AND COALESCE(quantity, -1) = COALESCE($6, -1)
                  AND COALESCE(cash_amount, -1) = COALESCE($7, -1)
                  AND paid_at >= $8
                ORDER BY paid_at DESC
                LIMIT 1
                """,
                guild_id,
                discord_user_id,
                recipient_torn_id,
                tax_type,
                item_id,
                quantity,
                cash_amount,
                since_dt,
            )
            return dict(row) if row else None

    async def create_receipt(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        recipient_torn_id: int,
        tax_type: str,
        item_id: Optional[int],
        quantity: Optional[int],
        cash_amount: Optional[int],
        torn_log_id: str,
        paid_at: datetime,
    ) -> int:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO host_tax_receipts (
                    guild_id,
                    discord_user_id,
                    recipient_torn_id,
                    tax_type,
                    item_id,
                    quantity,
                    cash_amount,
                    torn_log_id,
                    paid_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                RETURNING id
                """,
                guild_id,
                discord_user_id,
                recipient_torn_id,
                tax_type,
                item_id,
                quantity,
                cash_amount,
                torn_log_id,
                paid_at,
            )
            return int(row["id"])

    async def attach_latest_receipt_to_session(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        session_id: int,
        recipient_torn_id: int,
        tax_type: str,
        item_id: Optional[int],
        quantity: Optional[int],
        cash_amount: Optional[int],
        since_dt: datetime,
    ) -> None:
        async with self.acquire() as conn:
            await conn.execute(
                """
                UPDATE host_tax_receipts
                SET session_id = $3
                WHERE id = (
                    SELECT id
                    FROM host_tax_receipts
                    WHERE guild_id = $1
                      AND discord_user_id = $2
                      AND recipient_torn_id = $4
                      AND tax_type = $5
                      AND COALESCE(item_id, -1) = COALESCE($6, -1)
                      AND COALESCE(quantity, -1) = COALESCE($7, -1)
                      AND COALESCE(cash_amount, -1) = COALESCE($8, -1)
                      AND paid_at >= $9
                    ORDER BY paid_at DESC
                    LIMIT 1
                )
                """,
                guild_id,
                discord_user_id,
                session_id,
                recipient_torn_id,
                tax_type,
                item_id,
                quantity,
                cash_amount,
                since_dt,
            )
