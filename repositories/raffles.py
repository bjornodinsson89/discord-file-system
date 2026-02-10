from __future__ import annotations

from datetime import datetime
from typing import Optional, Any

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

    # ============================================================================
    # BACKGROUND WORKER METHODS (Added for events.py compatibility)
    # ============================================================================

    async def get_raffles_to_draw(self) -> list[dict[str, Any]]:
        """Get raffles that need to be drawn (ended but not drawn)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, guild_id, creator_discord_id, prize, ticket_payment_type,
                       ticket_price, tickets_available, max_tickets, max_tickets_per_user,
                       status, winner_discord_id, winner_torn_id, winning_ticket_number,
                       end_time, announcement_message_id, announcement_channel_id,
                       drawn_at, created_at, tickets_sold
                FROM raffles 
                WHERE status = 'active' 
                AND end_time < NOW()
                """
            )
            return [dict(row) for row in rows]

    async def draw_raffle_winner(self, raffle_id: int) -> Optional[dict[str, Any]]:
        """Draw a random winner for a raffle weighted by tickets purchased."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Get all paid entries for this raffle
                entries = await conn.fetch(
                    """
                    SELECT entry_id, raffle_id, discord_id, torn_user_id, num_tickets,
                           ticket_start, ticket_end, payment_verified
                    FROM raffle_entries 
                    WHERE raffle_id = $1 
                    AND payment_verified = TRUE
                    """,
                    raffle_id
                )
                
                if not entries:
                    # No entries - mark as completed with no winner
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET status = 'completed',
                            drawn_at = NOW()
                        WHERE raffle_id = $1
                        """,
                        raffle_id
                    )
                    return None

                # Calculate total tickets for weighted random
                total_tickets = sum(entry['num_tickets'] for entry in entries)
                
                # Generate random number between 1 and total tickets
                import random
                winning_number = random.randint(1, total_tickets)
                
                # Find which entry owns this ticket number
                current = 0
                winner_entry = None
                for entry in entries:
                    current += entry['num_tickets']
                    if winning_number <= current:
                        winner_entry = entry
                        break
                
                if winner_entry:
                    # Update raffle with winner info
                    await conn.execute(
                        """
                        UPDATE raffles 
                        SET winner_discord_id = $1,
                            winner_torn_id = $2,
                            winning_ticket_number = $3,
                            status = 'completed',
                            drawn_at = NOW()
                        WHERE raffle_id = $4
                        """,
                        winner_entry['discord_id'],
                        winner_entry['torn_user_id'],
                        winning_number,  # The actual winning ticket number
                        raffle_id
                    )
                    
                    return {
                        'discord_id': winner_entry['discord_id'],
                        'torn_user_id': winner_entry['torn_user_id'],
                        'ticket_number': winning_number,
                        'total_tickets': total_tickets
                    }
                
                return None

    async def update_raffle(self, raffle_id: int, **fields) -> bool:
        """Update raffle fields dynamically."""
        if not fields:
            return False
            
        async with self.pool.acquire() as conn:
            # Build SET clause dynamically
            set_clauses = []
            values = []
            for i, (key, value) in enumerate(fields.items(), 1):
                set_clauses.append(f"{key} = ${i}")
                values.append(value)
            
            # Add raffle_id as last parameter
            values.append(raffle_id)
            
            query = f"""
                UPDATE raffles 
                SET {', '.join(set_clauses)} 
                WHERE raffle_id = ${len(values)}
            """
            
            result = await conn.execute(query, *values)
            return 'UPDATE 1' in result

    async def get_raffle(self, raffle_id: int) -> Optional[dict[str, Any]]:
        """Get raffle by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raffle_id, guild_id, creator_discord_id, prize, ticket_payment_type,
                       ticket_price, ticket_payment_item_id, tickets_available, max_tickets,
                       max_tickets_per_user, status, winner_discord_id, winner_torn_id,
                       winning_ticket_number, end_time, announcement_message_id,
                       announcement_channel_id, drawn_at, created_at, tickets_sold
                FROM raffles
                WHERE raffle_id = $1
                """,
                raffle_id
            )
            return dict(row) if row else None

    async def get_raffle_entries(self, raffle_id: int) -> list[dict[str, Any]]:
        """Get all entries for a raffle."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entry_id, raffle_id, discord_id, torn_user_id, num_tickets,
                       ticket_start, ticket_end, payment_verified, reserved_until,
                       created_at, payment_verified_at
                FROM raffle_entries
                WHERE raffle_id = $1
                """,
                raffle_id
            )
            return [dict(row) for row in rows]

    async def cleanup_expired_raffle_entries(self) -> int:
        """Clean up expired raffle entry reservations and return count."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM raffle_entries 
                WHERE payment_verified = FALSE 
                AND reserved_until < NOW()
                """
            )
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0

    async def get_active_raffles(self, guild_id: Optional[int] = None) -> list[dict[str, Any]]:
        """Get active raffles, optionally filtered by guild."""
        async with self.pool.acquire() as conn:
            if guild_id:
                rows = await conn.fetch(
                    """
                    SELECT raffle_id, guild_id, creator_discord_id, prize, status,
                           end_time, tickets_sold, tickets_available
                    FROM raffles
                    WHERE status = 'active'
                    AND guild_id = $1
                    """,
                    guild_id
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT raffle_id, guild_id, creator_discord_id, prize, status,
                           end_time, tickets_sold, tickets_available
                    FROM raffles
                    WHERE status = 'active'
                    """
                )
            return [dict(row) for row in rows]

    async def count_entries(self, raffle_id: int) -> int:
        """Count number of entries for a raffle."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM raffle_entries
                WHERE raffle_id = $1
                """,
                raffle_id
            )
            return count or 0

    async def count_tickets_sold(self, raffle_id: int) -> int:
        """Count total tickets sold for a raffle."""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval(
                """
                SELECT COALESCE(SUM(num_tickets), 0) 
                FROM raffle_entries
                WHERE raffle_id = $1
                AND payment_verified = TRUE
                """,
                raffle_id
            )
            return total or 0

    async def mark_payment_verified(self, entry_id: int) -> bool:
        """Mark a raffle entry as paid."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE raffle_entries
                SET payment_verified = TRUE,
                    payment_verified_at = NOW(),
                    reserved_until = NULL
                WHERE entry_id = $1
                """,
                entry_id
            )
            return 'UPDATE 1' in result
