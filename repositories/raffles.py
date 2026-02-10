from __future__ import annotations

from datetime import datetime
from typing import Optional

from .base import RepositoryBase


class RafflesRepository(RepositoryBase):
    async def create_raffle(
        self,
        guild_id: int,
        creator_discord_id: int,
        prize: str,
        ticket_payment_type: str,
        ticket_price: int,
        tickets_available: int,
        max_tickets_per_user: int,
        end_time: Optional[datetime],
        end_trigger: str,
        hours_after_sold_out: Optional[float],
    ) -> int:
        """Create a new raffle."""
        async with self.pool.acquire() as conn:
            creator = await conn.fetchrow(
                "SELECT torn_user_id FROM user_api_keys WHERE discord_id = $1",
                creator_discord_id
            )
            creator_torn_id = creator["torn_user_id"] if creator else None
            
            row = await conn.fetchrow(
                """
                INSERT INTO raffles (
                    guild_id, creator_discord_id, creator_torn_id, prize, ticket_payment_type,
                    ticket_price, tickets_available, max_tickets_per_user,
                    end_time, end_trigger, hours_after_sold_out, status, is_free,
                    tickets_sold, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active', $12, 0, NOW())
                RETURNING raffle_id
                """,
                guild_id,
                creator_discord_id,
                creator_torn_id,
                prize,
                ticket_payment_type,
                0 if ticket_payment_type == "free" else ticket_price,
                tickets_available,
                max_tickets_per_user,
                end_time,
                end_trigger,
                hours_after_sold_out,
                ticket_payment_type == "free"
            )
            return int(row["raffle_id"])

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

    async def reserve_free_entry(self, raffle_id: int, discord_id: int, torn_user_id: int, num_tickets: int) -> Optional[dict]:
        """Reserve entry for free raffle - instantly confirmed, no payment needed."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO raffle_entries 
                        (raffle_id, discord_id, torn_user_id, num_tickets, 
                         reserved_until, payment_verified, payment_verified_at, created_at)
                    VALUES ($1, $2, $3, $4, NULL, TRUE, NOW(), NOW())
                    ON CONFLICT (raffle_id, discord_id) DO UPDATE
                    SET torn_user_id = EXCLUDED.torn_user_id,
                        num_tickets = raffle_entries.num_tickets + EXCLUDED.num_tickets,
                        payment_verified = TRUE,
                        payment_verified_at = NOW()
                    RETURNING *
                    """,
                    raffle_id, discord_id, torn_user_id, num_tickets
                )
                
                if row:
                    # Update tickets_sold count
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET tickets_sold = (
                            SELECT COALESCE(SUM(num_tickets), 0)
                            FROM raffle_entries
                            WHERE raffle_id = $1 AND payment_verified = TRUE
                        )
                        WHERE raffle_id = $1
                        """,
                        raffle_id
                    )
                    
                    # Check if sold out and move to drawing once
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET tickets_fully_sold_at = NOW()
                        WHERE raffle_id = $1 
                        AND end_trigger = 'tickets_sold'
                        AND tickets_fully_sold_at IS NULL
                        AND (
                            SELECT COALESCE(SUM(num_tickets), 0)
                            FROM raffle_entries
                            WHERE raffle_id = $1 AND payment_verified = TRUE
                        ) >= tickets_available
                        """,
                        raffle_id
                    )
                
                return dict(row) if row else None

    async def get_raffle(self, raffle_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT 
                    r.*,
                    COALESCE(
                        (SELECT SUM(num_tickets) FROM raffle_entries 
                         WHERE raffle_id = r.raffle_id AND payment_verified = TRUE), 0
                    ) as tickets_sold
                FROM raffles r
                WHERE r.raffle_id = $1
                """,
                raffle_id
            )
            return dict(row) if row else None

    async def get_active_raffles(self, guild_id: int) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    r.*,
                    COALESCE(
                        (SELECT SUM(num_tickets) FROM raffle_entries 
                         WHERE raffle_id = r.raffle_id AND payment_verified = TRUE), 0
                    ) as tickets_sold
                FROM raffles r
                WHERE r.guild_id = $1 AND r.status = 'active'
                ORDER BY r.created_at DESC
                """,
                guild_id
            )
            return [dict(row) for row in rows]

    async def get_raffle_entries(self, raffle_id: int) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM raffle_entries WHERE raffle_id = $1",
                raffle_id
            )
            return [dict(row) for row in rows]

    async def get_pending_verifications(self) -> list:
        """Get entries that need auto-verification."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM raffle_entries 
                WHERE payment_verified = FALSE 
                AND reserved_until > NOW()
                AND created_at <= NOW() - INTERVAL '4 minutes 30 seconds'
                """
            )
            return [dict(row) for row in rows]

    async def verify_payment_and_check_sold_out(self, entry_id: int, manual: bool = False):
        """Verify payment for an entry."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                entry = await conn.fetchrow(
                    "SELECT * FROM raffle_entries WHERE entry_id = $1",
                    entry_id
                )
                if not entry:
                    return False, None, "Entry not found"
                
                if entry["payment_verified"]:
                    return True, None, None
                
                if not manual and entry["reserved_until"] < datetime.utcnow():
                    return False, None, "Reservation expired"
                
                await conn.execute(
                    """
                    UPDATE raffle_entries 
                    SET payment_verified = TRUE, payment_verified_at = NOW()
                    WHERE entry_id = $1
                    """,
                    entry_id
                )
                
                await conn.execute(
                    """
                    UPDATE raffles 
                    SET tickets_sold = (
                        SELECT COALESCE(SUM(num_tickets), 0)
                        FROM raffle_entries
                        WHERE raffle_id = $1 AND payment_verified = TRUE
                    )
                    WHERE raffle_id = $1
                    """,
                    entry["raffle_id"]
                )
                
                raffle = await conn.fetchrow(
                    "SELECT * FROM raffles WHERE raffle_id = $1",
                    entry["raffle_id"]
                )
                
                sold_out_id = None
                if (raffle["end_trigger"] == "tickets_sold" and 
                    raffle["tickets_fully_sold_at"] is None):
                    
                    total_sold = await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(num_tickets), 0)
                        FROM raffle_entries
                        WHERE raffle_id = $1 AND payment_verified = TRUE
                        """,
                        entry["raffle_id"]
                    )
                    
                    if total_sold >= raffle["tickets_available"]:
                        update_result = await conn.execute(
                            """
                            UPDATE raffles 
                            SET tickets_fully_sold_at = NOW()
                            WHERE raffle_id = $1
                            AND tickets_fully_sold_at IS NULL
                            """,
                            entry["raffle_id"]
                        )
                        if update_result == "UPDATE 1":
                            sold_out_id = entry["raffle_id"]
                
                return True, sold_out_id, None

    async def cancel_expired_reservation(self, entry_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM raffle_entries WHERE entry_id = $1 AND payment_verified = FALSE",
                entry_id
            )

    async def cleanup_expired_raffle_entries(self) -> int:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM raffle_entries 
                WHERE payment_verified = FALSE 
                AND reserved_until < NOW()
                """
            )
            return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def draw_raffle_winner(self, raffle_id: int) -> Optional[dict]:
        """Draw a winner for the raffle."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                entries = await conn.fetch(
                    """
                    SELECT discord_id, torn_user_id, num_tickets
                    FROM raffle_entries
                    WHERE raffle_id = $1 AND payment_verified = TRUE
                    """,
                    raffle_id
                )
                
                if not entries:
                    await conn.execute(
                        "UPDATE raffles SET status = 'cancelled' WHERE raffle_id = $1",
                        raffle_id
                    )
                    return None
                
                import random
                pool = []
                for entry in entries:
                    pool.extend([entry] * entry["num_tickets"])
                
                winner = random.choice(pool)
                
                await conn.execute(
                    """
                    UPDATE raffles 
                    SET status = 'completed', 
                        winner_discord_id = $1,
                        winner_torn_id = $2,
                        drawn_at = NOW()
                    WHERE raffle_id = $3
                    """,
                    winner["discord_id"],
                    winner["torn_user_id"],
                    raffle_id
                )
                
                return {
                    "discord_id": winner["discord_id"],
                    "torn_user_id": winner["torn_user_id"],
                    "total_entries": len(entries),
                    "total_tickets": sum(e["num_tickets"] for e in entries)
                }

    async def get_raffles_to_draw(self) -> list:
        """Get raffles that are ready to be drawn."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM raffles
                WHERE status = 'active'
                  AND end_trigger = 'tickets_sold'
                  AND tickets_fully_sold_at IS NOT NULL
                  AND NOW() >= tickets_fully_sold_at + INTERVAL '30 seconds'
                """
            )
            return [dict(row) for row in rows]
