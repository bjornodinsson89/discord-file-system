from __future__ import annotations

import random
from datetime import datetime
from typing import Optional, Any

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
        hours_after_sold_out: Optional[int],
    ) -> int:
        """Create a new raffle with sell-out trigger support."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO raffles (
                    guild_id, creator_discord_id, prize, ticket_payment_type,
                    ticket_price, tickets_available, max_tickets_per_user,
                    end_time, end_trigger, hours_after_sold_out, status,
                    tickets_sold, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'active', 0, NOW())
                RETURNING raffle_id
                """,
                guild_id,
                creator_discord_id,
                prize,
                ticket_payment_type,
                ticket_price,
                tickets_available,
                max_tickets_per_user,
                end_time,
                end_trigger,
                hours_after_sold_out,
            )
            return int(row["raffle_id"])

    async def get_raffles_to_draw(self) -> list[dict[str, Any]]:
        """Get raffles ready to draw (time-ended OR sell-out + delay passed)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, guild_id, creator_discord_id, prize, 
                       ticket_payment_type, ticket_price, tickets_available,
                       max_tickets_per_user, status, winner_discord_id,
                       end_time, end_trigger, hours_after_sold_out, 
                       tickets_fully_sold_at, tickets_sold
                FROM raffles 
                WHERE status = 'active' 
                AND (
                    -- Time-based end
                    (end_trigger = 'time' OR end_trigger IS NULL) 
                    AND end_time <= NOW()
                    OR
                    -- Sell-out + delay end
                    end_trigger = 'tickets_sold' 
                    AND tickets_fully_sold_at IS NOT NULL
                    AND tickets_fully_sold_at + (hours_after_sold_out || ' hours')::INTERVAL <= NOW()
                )
                """
            )
            return [dict(row) for row in rows]

    async def verify_payment_and_check_sold_out(
        self, entry_id: int
    ) -> tuple[bool, Optional[int]]:
        """Verify payment and return (success, raffle_id_if_sold_out)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Verify payment
                row = await conn.fetchrow(
                    """
                    UPDATE raffle_entries 
                    SET payment_verified = TRUE, 
                        payment_verified_at = NOW(),
                        reserved_until = NULL
                    WHERE entry_id = $1
                    RETURNING raffle_id
                    """,
                    entry_id,
                )
                
                if not row:
                    return False, None
                
                raffle_id = row["raffle_id"]
                
                # Check if this caused sell-out
                result = await conn.fetchrow(
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
                    RETURNING raffle_id
                    """,
                    raffle_id,
                )
                
                return True, raffle_id if result else None

    async def draw_raffle_winner(
        self, raffle_id: int
    ) -> Optional[dict[str, Any]]:
        """Draw weighted random winner."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                entries = await conn.fetch(
                    """
                    SELECT entry_id, discord_id, torn_user_id, num_tickets
                    FROM raffle_entries
                    WHERE raffle_id = $1 AND payment_verified = TRUE
                    """,
                    raffle_id,
                )
                
                if not entries:
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET status = 'completed', drawn_at = NOW()
                        WHERE raffle_id = $1
                        """,
                        raffle_id,
                    )
                    return None
                
                # Weighted random selection
                total_tickets = sum(e["num_tickets"] for e in entries)
                winning_number = random.randint(1, total_tickets)
                
                current = 0
                winner = None
                for entry in entries:
                    current += entry["num_tickets"]
                    if winning_number <= current:
                        winner = entry
                        break
                
                if winner:
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET winner_discord_id = $1,
                            winner_torn_id = $2,
                            winner_ticket_number = $3,
                            status = 'completed',
                            drawn_at = NOW()
                        WHERE raffle_id = $4
                        """,
                        winner["discord_id"],
                        winner["torn_user_id"],
                        winning_number,
                        raffle_id,
                    )
                    
                    return {
                        "discord_id": winner["discord_id"],
                        "torn_user_id": winner["torn_user_id"],
                        "ticket_number": winning_number,
                        "total_tickets": total_tickets,
                    }
                
                return None

    async def reserve_entry(
        self, raffle_id: int, discord_id: int, torn_user_id: int,
        num_tickets: int, reserved_until: datetime
    ) -> Optional[dict]:
        """Reserve entry (existing method preserved)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO raffle_entries 
                    (raffle_id, discord_id, torn_user_id, num_tickets, 
                     reserved_until, payment_verified)
                VALUES ($1, $2, $3, $4, $5, FALSE)
                ON CONFLICT (raffle_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    num_tickets = EXCLUDED.num_tickets,
                    reserved_until = EXCLUDED.reserved_until,
                    payment_verified = raffle_entries.payment_verified,
                    payment_verified_at = raffle_entries.payment_verified_at
                RETURNING *
                """,
                raffle_id, discord_id, torn_user_id, num_tickets, reserved_until
            )
            return dict(row) if row else None

    async def get_raffle(self, raffle_id: int) -> Optional[dict[str, Any]]:
        """Get single raffle by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raffle_id, guild_id, creator_discord_id, prize,
                       ticket_payment_type, ticket_price, tickets_available,
                       max_tickets_per_user, status, winner_discord_id,
                       end_time, end_trigger, hours_after_sold_out,
                       tickets_fully_sold_at, tickets_sold, created_at
                FROM raffles WHERE raffle_id = $1
                """,
                raffle_id,
            )
            return dict(row) if row else None

    async def get_raffle_entries(self, raffle_id: int) -> list[dict[str, Any]]:
        """Get all entries for a raffle."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entry_id, discord_id, torn_user_id, num_tickets,
                       payment_verified, reserved_until, created_at
                FROM raffle_entries WHERE raffle_id = $1
                ORDER BY created_at ASC
                """,
                raffle_id,
            )
            return [dict(row) for row in rows]

    async def update_raffle(self, raffle_id: int, **fields) -> bool:
        """Update raffle fields dynamically."""
        if not fields:
            return False
            
        async with self.pool.acquire() as conn:
            sets = []
            values = []
            for i, (key, value) in enumerate(fields.items(), 1):
                sets.append(f"{key} = ${i}")
                values.append(value)
            values.append(raffle_id)
            
            query = f"UPDATE raffles SET {', '.join(sets)} WHERE raffle_id = ${len(values)}"
            result = await conn.execute(query, *values)
            return "UPDATE 1" in result
