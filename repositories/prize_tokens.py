from __future__ import annotations

import json
from typing import Any

import asyncpg

from .base import RepositoryBase


class PrizeTokensRepository(RepositoryBase):
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
        if conn is not None:
            return await self._apply_transaction_with_conn(
                conn=conn,
                guild_id=guild_id,
                user_id=user_id,
                transaction_type=transaction_type,
                amount=amount,
                source_type=source_type,
                source_id=source_id,
                dedupe_key=dedupe_key,
                metadata=metadata,
            )

        async with self.acquire() as local_conn:
            async with local_conn.transaction():
                return await self._apply_transaction_with_conn(
                    conn=local_conn,
                    guild_id=guild_id,
                    user_id=user_id,
                    transaction_type=transaction_type,
                    amount=amount,
                    source_type=source_type,
                    source_id=source_id,
                    dedupe_key=dedupe_key,
                    metadata=metadata,
                )

    async def _apply_transaction_with_conn(
        self,
        *,
        conn: asyncpg.Connection,
        guild_id: int,
        user_id: int,
        transaction_type: str,
        amount: int,
        source_type: str,
        source_id: str,
        dedupe_key: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict | None:
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
        balance = int(profile.get("prize_token_balance") or 0)
        next_balance = balance + int(amount)
        if next_balance < 0:
            raise ValueError("insufficient prize token balance")

        earned_delta = amount if amount > 0 else 0
        spent_delta = abs(amount) if amount < 0 else 0

        tx = await conn.fetchrow(
            """
            INSERT INTO prize_token_transactions (
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
            SET prize_token_balance = $3,
                prize_token_lifetime_earned = prize_token_lifetime_earned + $4,
                prize_token_lifetime_spent = prize_token_lifetime_spent + $5,
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

    async def get_recent_transactions(
        self, guild_id: int, user_id: int, limit: int = 10
    ) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM prize_token_transactions WHERE guild_id = $1 AND user_id = $2 ORDER BY id DESC LIMIT $3",
                guild_id,
                user_id,
                limit,
            )
            return [dict(r) for r in rows]

    async def cleanup_departed_member(self, guild_id: int, user_id: int) -> dict[str, int]:
        async with self.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM prize_token_transactions WHERE guild_id = $1 AND user_id = $2",
                guild_id,
                user_id,
            )
            return {"prize_token_transactions": int(str(result).split()[-1])}

    async def list_guild_user_ids(self, guild_id: int) -> set[int]:
        async with self.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT user_id FROM prize_token_transactions WHERE guild_id = $1", guild_id)
            return {int(r["user_id"]) for r in rows if int(r["user_id"] or 0) > 0}
