from __future__ import annotations

from typing import Optional

from .base import RepositoryBase


class JumpsRepository(RepositoryBase):
    async def get_settings(self, guild_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_settings WHERE guild_id = $1", guild_id)
            return dict(row) if row else None

    async def upsert_settings(
        self,
        *,
        guild_id: int,
        host_role_id: int,
        announce_channel_id: Optional[int],
        payee_discord_id: Optional[int],
        currency_default: str,
        default_max_slots: int,
    ) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jump_99k_settings (
                    guild_id, host_role_id, announce_channel_id, payee_discord_id, currency_default, default_max_slots, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET
                    host_role_id = EXCLUDED.host_role_id,
                    announce_channel_id = EXCLUDED.announce_channel_id,
                    payee_discord_id = EXCLUDED.payee_discord_id,
                    currency_default = EXCLUDED.currency_default,
                    default_max_slots = EXCLUDED.default_max_slots,
                    updated_at = NOW()
                RETURNING *
                """,
                guild_id,
                host_role_id,
                announce_channel_id,
                payee_discord_id,
                currency_default,
                default_max_slots,
            )
            return dict(row)

    async def create_session(
        self,
        *,
        guild_id: int,
        host_discord_id: int,
        title: str,
        scheduled_start_text: Optional[str],
        max_slots: int,
        notes: Optional[str],
        announce_channel_id: Optional[int],
        announce_message_id: Optional[int],
    ) -> int:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO jump_99k_sessions (
                    guild_id, host_discord_id, title, scheduled_start_text, max_slots, notes, status, announce_channel_id, announce_message_id
                ) VALUES ($1,$2,$3,$4,$5,$6,'open',$7,$8)
                RETURNING id
                """,
                guild_id,
                host_discord_id,
                title,
                scheduled_start_text,
                max_slots,
                notes,
                announce_channel_id,
                announce_message_id,
            )
            return int(row["id"])

    async def set_announcement_message(self, session_id: int, *, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE jump_99k_sessions SET announce_channel_id = $2, announce_message_id = $3 WHERE id = $1",
                session_id,
                channel_id,
                message_id,
            )

    async def get_active_session(self, guild_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM jump_99k_sessions WHERE guild_id = $1 AND status = 'open' ORDER BY created_at DESC LIMIT 1",
                guild_id,
            )
            return dict(row) if row else None

    async def get_session(self, session_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jump_99k_sessions WHERE id = $1", session_id)
            return dict(row) if row else None

    async def signup_count(self, session_id: int) -> int:
        async with self.pool.acquire() as conn:
            return int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jump_99k_signups WHERE session_id = $1 AND status IN ('signed_up','completed','not_completed')",
                    session_id,
                )
            )

    async def create_or_restore_signup(
        self,
        *,
        session_id: int,
        guild_id: int,
        discord_id: int,
        torn_user_id: Optional[int],
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jump_99k_signups (session_id, guild_id, discord_id, torn_user_id, status)
                VALUES ($1,$2,$3,$4,'signed_up')
                ON CONFLICT (session_id, discord_id)
                DO UPDATE SET status = 'signed_up', torn_user_id = EXCLUDED.torn_user_id
                """,
                session_id,
                guild_id,
                discord_id,
                torn_user_id,
            )

    async def cancel_signup(self, *, session_id: int, discord_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jump_99k_signups SET status = 'cancelled' WHERE session_id = $1 AND discord_id = $2 RETURNING id",
                session_id,
                discord_id,
            )
            return row is not None

    async def list_signups(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM jump_99k_signups WHERE session_id = $1 ORDER BY signed_up_at ASC",
                session_id,
            )
            return [dict(r) for r in rows]

    async def close_session_and_record(
        self,
        *,
        session_id: int,
        guild_id: int,
        completed_discord_ids: list[int],
        not_completed_discord_ids: list[int],
    ) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "UPDATE jump_99k_sessions SET status = 'closed', closed_at = NOW() WHERE id = $1 AND status = 'open' RETURNING id",
                    session_id,
                )
                if not row:
                    return False

                if completed_discord_ids:
                    await conn.execute(
                        "UPDATE jump_99k_signups SET status = 'completed' WHERE session_id = $1 AND discord_id = ANY($2::bigint[])",
                        session_id,
                        completed_discord_ids,
                    )
                if not_completed_discord_ids:
                    await conn.execute(
                        "UPDATE jump_99k_signups SET status = 'not_completed' WHERE session_id = $1 AND discord_id = ANY($2::bigint[])",
                        session_id,
                        not_completed_discord_ids,
                    )

                completed_count = len(completed_discord_ids)
                not_completed_count = len(not_completed_discord_ids)
                await conn.execute(
                    """
                    INSERT INTO jump_99k_totals (guild_id, completed_count, not_completed_count, updated_at)
                    VALUES ($1,$2,$3,NOW())
                    ON CONFLICT (guild_id)
                    DO UPDATE SET
                        completed_count = jump_99k_totals.completed_count + EXCLUDED.completed_count,
                        not_completed_count = jump_99k_totals.not_completed_count + EXCLUDED.not_completed_count,
                        updated_at = NOW()
                    """,
                    guild_id,
                    completed_count,
                    not_completed_count,
                )

                await conn.execute("DELETE FROM jump_99k_readiness WHERE session_id = $1", session_id)
                await conn.execute("DELETE FROM jump_99k_signups WHERE session_id = $1", session_id)
                return True

    async def get_totals(self, guild_id: int) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM jump_99k_totals WHERE guild_id = $1",
                guild_id,
            )
            if not row:
                return {"guild_id": guild_id, "completed_count": 0, "not_completed_count": 0}
            return dict(row)

    async def list_signups_with_receipts(self, session_id: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.discord_id,
                       s.status AS signup_status,
                       r.id AS receipt_id,
                       r.status AS receipt_status,
                       r.amount,
                       r.currency,
                       r.created_at AS receipt_created_at
                FROM jump_99k_signups s
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM payment_receipts pr
                    WHERE pr.feature_type = '99k_jump'
                      AND pr.feature_ref_id = $1::text
                      AND pr.payer_discord_id = s.discord_id
                    ORDER BY pr.created_at DESC
                    LIMIT 1
                ) r ON TRUE
                WHERE s.session_id = $1
                ORDER BY s.signed_up_at
                """,
                session_id,
            )
            return [dict(r) for r in rows]


    # Compatibility methods for existing handlers
    async def get_jump_session(self, session_id: int) -> Optional[dict]:
        return await self.get_session(session_id)

    async def list_sessions(self, *, guild_id: int, status: Optional[str], limit: int, offset: int) -> list[dict]:
        async with self.pool.acquire() as conn:
            if status:
                rows = await conn.fetch("SELECT * FROM jump_99k_sessions WHERE guild_id=$1 AND status=$2 ORDER BY created_at DESC LIMIT $3 OFFSET $4", guild_id, status, limit, offset)
            else:
                rows = await conn.fetch("SELECT * FROM jump_99k_sessions WHERE guild_id=$1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", guild_id, limit, offset)
            return [dict(r) for r in rows]

    async def lock_session(self, session_id: int) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute("UPDATE jump_99k_sessions SET status='closed', closed_at=NOW() WHERE id=$1 AND status='open'", session_id)
            return result.endswith('1')

    async def cancel_session(self, session_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE jump_99k_sessions SET status='closed', closed_at=NOW() WHERE id=$1 AND status='open' RETURNING id, guild_id", session_id)
            if not row:
                return False
            await conn.execute("DELETE FROM jump_99k_readiness WHERE session_id=$1", session_id)
            await conn.execute("DELETE FROM jump_99k_signups WHERE session_id=$1", session_id)
            return True

    async def complete_session(self, session_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE jump_99k_sessions SET status='closed', closed_at=NOW() WHERE id=$1 AND status='open' RETURNING id, guild_id", session_id)
            if not row:
                return False
            await conn.execute("INSERT INTO jump_99k_totals (guild_id, completed_count, not_completed_count, updated_at) VALUES ($1,1,0,NOW()) ON CONFLICT (guild_id) DO UPDATE SET completed_count=jump_99k_totals.completed_count+1, updated_at=NOW()", int(row['guild_id']))
            await conn.execute("DELETE FROM jump_99k_readiness WHERE session_id=$1", session_id)
            await conn.execute("DELETE FROM jump_99k_signups WHERE session_id=$1", session_id)
            return True

    async def mark_purchase_verified(self, session_id: int, discord_id: int) -> bool:
        return True
