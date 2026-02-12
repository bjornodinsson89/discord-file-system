from __future__ import annotations

from datetime import datetime
from typing import Optional

import asyncpg

from .base import RepositoryBase


class JumpsRepository(RepositoryBase):
    async def reserve_signup(self, session_id: int, discord_id: int, torn_user_id: int, reserved_until: datetime) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO happy_jump_signups (session_id, discord_id, torn_user_id, status, reserved_until)
                VALUES ($1, $2, $3, 'reserved', $4)
                ON CONFLICT (session_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    status = CASE
                        WHEN happy_jump_signups.payment_verified = TRUE THEN happy_jump_signups.status
                        ELSE 'reserved'
                    END,
                    reserved_until = CASE
                        WHEN happy_jump_signups.payment_verified = TRUE THEN happy_jump_signups.reserved_until
                        ELSE EXCLUDED.reserved_until
                    END
                RETURNING id
                """,
                session_id,
                discord_id,
                torn_user_id,
                reserved_until,
            )
            return int(row["id"])

    async def confirm_signup(self, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE happy_jump_signups
                SET status = 'confirmed', payment_verified = TRUE,
                    payment_verified_at = NOW(), purchase_verified_at = NOW(), reserved_until = NULL
                WHERE session_id = $1 AND discord_id = $2
                RETURNING id
                """,
                session_id,
                discord_id,
            )
            return row is not None

    async def mark_purchase_verified(self, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE happy_jump_signups
                SET status = 'paid', payment_verified = TRUE,
                    payment_verified_at = NOW(), purchase_verified_at = NOW(), reserved_until = NULL
                WHERE session_id = $1 AND discord_id = $2
                RETURNING id, purchase_verified_at
                """,
                session_id,
                discord_id,
            )
            return row is not None

    async def add_to_waitlist(self, session_id: int, discord_id: int, torn_user_id: int) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO happy_jump_waitlist (session_id, discord_id, torn_user_id, position)
                SELECT $1, $2, $3, COALESCE(MAX(position), 0) + 1
                FROM happy_jump_waitlist
                WHERE session_id = $1
                ON CONFLICT (session_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id
                RETURNING position
                """,
                session_id,
                discord_id,
                torn_user_id,
            )
            return int(row["position"])
