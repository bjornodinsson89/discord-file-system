from __future__ import annotations

from datetime import datetime
from typing import Optional

from .base import RepositoryBase


class JumpsRepository(RepositoryBase):
    async def create_session(
        self,
        *,
        guild_id: int,
        host_discord_id: int,
        host_torn_id: int,
        jump_type: str,
        max_spots: int,
        xanax_count: int,
        payment_type: str,
        payment_amount: int,
        payment_item_id: Optional[int] = None,
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO happy_jump_sessions (
                    guild_id, host_discord_id, host_torn_id, jump_type,
                    max_spots, xanax_count, payment_type, payment_amount,
                    payment_item_id, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'open')
                RETURNING id
                """,
                guild_id,
                host_discord_id,
                host_torn_id,
                jump_type,
                max_spots,
                xanax_count,
                payment_type,
                payment_amount,
                payment_item_id,
            )
            return int(row["id"])

    async def get_jump_session(self, session_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM happy_jump_sessions WHERE id = $1", session_id)
            return dict(row) if row else None

    async def list_sessions(self, *, guild_id: int, status: Optional[str], limit: int, offset: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT * FROM happy_jump_sessions
                    WHERE guild_id = $1 AND status = $2
                    ORDER BY created_at DESC
                    LIMIT $3 OFFSET $4
                    """,
                    guild_id,
                    status,
                    limit,
                    offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM happy_jump_sessions
                    WHERE guild_id = $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    guild_id,
                    limit,
                    offset,
                )
            return [dict(r) for r in rows]

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

    async def get_session_signups(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM happy_jump_signups WHERE session_id = $1 ORDER BY signed_up_at",
                session_id,
            )
            return [dict(r) for r in rows]

    async def get_signup(self, session_id: int, discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM happy_jump_signups WHERE session_id = $1 AND discord_id = $2",
                session_id,
                discord_id,
            )
            return dict(row) if row else None

    async def get_session_readiness(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM happy_jump_readiness WHERE session_id = $1", session_id)
            return [dict(r) for r in rows]

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

    async def lock_session(self, session_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE happy_jump_sessions SET status = 'locked', updated_at = NOW() WHERE id = $1 AND status = 'open'",
                session_id,
            )
            return result.endswith("1")

    async def _cleanup_session_ephemeral(self, conn, session_id: int) -> None:
        await conn.execute("DELETE FROM happy_jump_readiness WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM happy_jump_waitlist WHERE session_id = $1", session_id)
        await conn.execute("DELETE FROM happy_jump_signups WHERE session_id = $1", session_id)

    async def _increment_session_summary(self, conn, final_status: str) -> None:
        await conn.execute(
            """
            INSERT INTO jump_99k_session_totals (summary_key, completed_count, not_completed_count)
            VALUES ('global', $1, $2)
            ON CONFLICT (summary_key) DO UPDATE
            SET completed_count = jump_99k_session_totals.completed_count + EXCLUDED.completed_count,
                not_completed_count = jump_99k_session_totals.not_completed_count + EXCLUDED.not_completed_count,
                updated_at = NOW()
            """,
            1 if final_status == "completed" else 0,
            0 if final_status == "completed" else 1,
        )

    async def _close_session(self, session_id: int, *, status: str) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    UPDATE happy_jump_sessions
                    SET status = $2,
                        completed_at = CASE WHEN $2 = 'completed' THEN NOW() ELSE completed_at END,
                        updated_at = NOW()
                    WHERE id = $1 AND status IN ('open', 'locked')
                    RETURNING id
                    """,
                    session_id,
                    status,
                )
                if not row:
                    return False

                await self._cleanup_session_ephemeral(conn, session_id)
                await self._increment_session_summary(conn, status)
                return True

    async def cancel_session(self, session_id: int) -> bool:
        return await self._close_session(session_id, status="cancelled")

    async def complete_session(self, session_id: int) -> bool:
        return await self._close_session(session_id, status="completed")
