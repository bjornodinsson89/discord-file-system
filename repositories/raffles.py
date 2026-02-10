from __future__ import annotations

from datetime import datetime

from .base import RepositoryBase


class RafflesRepository(RepositoryBase):
    async def reserve_entry(self, raffle_id: int, discord_id: int, torn_user_id: int, num_tickets: int, reserved_until: datetime):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO raffle_entries (raffle_id, discord_id, torn_user_id, num_tickets, reserved_until, payment_verified)
                VALUES ($1, $2, $3, $4, $5, FALSE)
                ON CONFLICT (raffle_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    num_tickets = EXCLUDED.num_tickets,
                    reserved_until = EXCLUDED.reserved_until,
                    payment_verified = raffle_entries.payment_verified,
                    payment_verified_at = raffle_entries.payment_verified_at
                RETURNING *
                """,
                raffle_id,
                discord_id,
                torn_user_id,
                num_tickets,
                reserved_until,
            )
            return dict(row) if row else None
