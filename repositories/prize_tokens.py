from __future__ import annotations

import json
from typing import Any

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
    ) -> dict | None:
        if amount == 0:
            return None
        async with self.acquire() as conn:
            async with conn.transaction():
                dup = await conn.fetchrow(
                    "SELECT id FROM prize_token_transactions WHERE guild_id = $1 AND dedupe_key = $2",
                    guild_id,
                    dedupe_key,
                )
                if dup:
                    return None

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

    async def get_recent_transactions(self, guild_id: int, user_id: int, limit: int = 10) -> list[dict]:
        async with self.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM prize_token_transactions WHERE guild_id = $1 AND user_id = $2 ORDER BY id DESC LIMIT $3",
                guild_id,
                user_id,
                limit,
            )
            return [dict(r) for r in rows]
