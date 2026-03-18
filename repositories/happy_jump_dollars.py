from __future__ import annotations

import json
from typing import Any

import asyncpg

from .base import RepositoryBase


class HappyJumpDollarRepository(RepositoryBase):
    async def apply_transaction(
        self,
        *,
        guild_id: int,
        user_id: int,
        transaction_type: str,
        amount: int,
        source_type: str,
        source_id: str,
        dedupe_key: str,
        metadata: dict[str, Any] | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> dict | None:
        if amount == 0:
            return None
        if conn is None:
            async with self.acquire() as local_conn:
                async with local_conn.transaction():
                    return await self.apply_transaction(
                        guild_id=guild_id,
                        user_id=user_id,
                        transaction_type=transaction_type,
                        amount=amount,
                        source_type=source_type,
                        source_id=source_id,
                        dedupe_key=dedupe_key,
                        metadata=metadata,
                        conn=local_conn,
                    )

        await conn.execute(
            """
            INSERT INTO engagement_profiles (guild_id, user_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id, user_id) DO NOTHING
            """,
            guild_id,
            user_id,
        )
        profile = await conn.fetchrow(
            "SELECT * FROM engagement_profiles WHERE guild_id = $1 AND user_id = $2 FOR UPDATE",
            guild_id,
            user_id,
        )
        balance = int(profile.get("hjd_balance") or 0)
        next_balance = balance + int(amount)
        if next_balance < 0:
            raise ValueError("insufficient HJD balance")
        earned_delta = amount if amount > 0 else 0
        spent_delta = abs(amount) if amount < 0 else 0
        tx = await conn.fetchrow(
            """
            INSERT INTO happy_jump_dollar_transactions (
                guild_id, user_id, transaction_type, amount, balance_after,
                source_type, source_id, dedupe_key, metadata_json
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
            ON CONFLICT (guild_id, dedupe_key) DO NOTHING
            RETURNING *
            """,
            guild_id,
            user_id,
            transaction_type,
            amount,
            next_balance,
            source_type,
            source_id,
            dedupe_key,
            json.dumps(metadata or {}),
        )
        if tx is None:
            return None
        await conn.execute(
            """
            UPDATE engagement_profiles
            SET hjd_balance = $3,
                hjd_lifetime_earned = hjd_lifetime_earned + $4,
                hjd_lifetime_spent = hjd_lifetime_spent + $5,
                updated_at = NOW()
            WHERE guild_id = $1 AND user_id = $2
            """,
            guild_id,
            user_id,
            next_balance,
            earned_delta,
            spent_delta,
        )
        return dict(tx)
