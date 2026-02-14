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
        is_bundle: bool = False,
        bundle_text: Optional[str] = None,
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
                    tickets_sold, is_bundle, bundle_text, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active', $12, 0, $13, $14, NOW())
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
                ticket_payment_type == "free",
                is_bundle,
                bundle_text,
            )
            return int(row["raffle_id"])

    async def reserve_entry(self, raffle_id: int, discord_id: int, torn_user_id: int, num_tickets: int, reserved_until: datetime):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO raffle_entries (
                    raffle_id,
                    discord_id,
                    torn_user_id,
                    num_tickets,
                    reserved_until,
                    payment_verified,
                    created_at
                )
                VALUES ($1, $2, $3, $4, $5, FALSE, NOW())
                ON CONFLICT (raffle_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    num_tickets = EXCLUDED.num_tickets,
                    reserved_until = EXCLUDED.reserved_until
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


    async def cancel_active_raffle(self, raffle_id: int) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE raffles SET status = 'cancelled' WHERE raffle_id = $1 AND status = 'active' RETURNING raffle_id",
                raffle_id,
            )
            return row is not None
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


    async def get_all_active_raffle_ids(self) -> list[int]:
        """Get raffle IDs for all active raffles."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id
                FROM raffles
                WHERE status = 'active'
                """
            )
            return [int(row["raffle_id"]) for row in rows]

    async def get_active_raffles_with_panels(self) -> list:
        """Get active raffles that already have purchase panel message references."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT raffle_id, purchase_panel_channel_id, purchase_panel_message_id
                FROM raffles
                WHERE status = 'active'
                  AND purchase_panel_channel_id IS NOT NULL
                  AND purchase_panel_message_id IS NOT NULL
                """
            )
            return [dict(row) for row in rows]

    async def get_raffle_entries(self, raffle_id: int) -> list:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM raffle_entries WHERE raffle_id = $1",
                raffle_id
            )
            return [dict(row) for row in rows]



    async def get_entry_by_raffle_and_discord(self, raffle_id: int, discord_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM raffle_entries WHERE raffle_id = $1 AND discord_id = $2",
                raffle_id,
                discord_id,
            )
            return dict(row) if row else None

    async def get_reserved_tickets_count(self, raffle_id: int) -> int:
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT COALESCE(SUM(num_tickets), 0)
                FROM raffle_entries
                WHERE raffle_id = $1
                  AND payment_verified = FALSE
                  AND reserved_until > NOW()
                """,
                raffle_id,
            )
            return int(value or 0)

    async def draw_winner(self, raffle_id: int) -> Optional[dict]:
        return await self.draw_raffle_winner(raffle_id)
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

    async def get_entry_with_raffle(self, entry_id: int) -> Optional[dict]:
        """Fetch reservation and raffle details used for payment verification."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    e.entry_id,
                    e.raffle_id,
                    e.discord_id,
                    e.torn_user_id,
                    e.num_tickets,
                    e.reserved_until,
                    e.payment_verified,
                    e.created_at,
                    r.creator_discord_id,
                    r.creator_torn_id,
                    COALESCE(r.creator_torn_id, u.torn_user_id) AS effective_creator_torn_id,
                    r.ticket_payment_type,
                    r.ticket_price,
                    r.tickets_available,
                    r.end_trigger,
                    r.tickets_fully_sold_at,
                    r.status
                FROM raffle_entries e
                JOIN raffles r ON r.raffle_id = e.raffle_id
                LEFT JOIN user_api_keys u ON u.discord_id = r.creator_discord_id
                WHERE e.entry_id = $1
                """,
                entry_id,
            )
            return dict(row) if row else None

    async def get_entry_for_verification(self, entry_id: int) -> Optional[dict]:
        """Backward-compatible alias for entry+raffle verification lookup."""
        return await self.get_entry_with_raffle(entry_id)

    async def mark_entry_verified(self, entry_id: int) -> bool:
        """Mark a reservation as paid after external verification succeeds."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE raffle_entries
                SET payment_verified = TRUE,
                    payment_verified_at = NOW()
                WHERE entry_id = $1
                  AND payment_verified = FALSE
                """,
                entry_id,
            )
            return result == "UPDATE 1"

    async def recompute_tickets_sold_and_maybe_set_sold_out(self, raffle_id: int) -> Optional[int]:
        """Recompute verified ticket totals and set sold-out timestamp when threshold is reached."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE raffles
                    SET tickets_sold = (
                        SELECT COALESCE(SUM(num_tickets), 0)
                        FROM raffle_entries
                        WHERE raffle_id = $1
                          AND payment_verified = TRUE
                    )
                    WHERE raffle_id = $1
                    """,
                    raffle_id,
                )

                row = await conn.fetchrow(
                    """
                    SELECT raffle_id, tickets_available, tickets_sold, end_trigger, tickets_fully_sold_at
                    FROM raffles
                    WHERE raffle_id = $1
                    """,
                    raffle_id,
                )
                if not row:
                    return None

                if (
                    row["end_trigger"] == "tickets_sold"
                    and row["tickets_fully_sold_at"] is None
                    and int(row["tickets_sold"] or 0) >= int(row["tickets_available"] or 0)
                ):
                    result = await conn.execute(
                        """
                        UPDATE raffles
                        SET tickets_fully_sold_at = NOW()
                        WHERE raffle_id = $1
                          AND tickets_fully_sold_at IS NULL
                        """,
                        raffle_id,
                    )
                    if result == "UPDATE 1":
                        return raffle_id

                return None

    async def recompute_tickets_sold_and_set_sold_out(self, raffle_id: int) -> Optional[int]:
        """Backward-compatible alias for sold-out recompute logic."""
        return await self.recompute_tickets_sold_and_maybe_set_sold_out(raffle_id)

    async def set_purchase_panel_ref(self, raffle_id: int, channel_id: int, message_id: int) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET purchase_panel_channel_id = $2,
                    purchase_panel_message_id = $3
                WHERE raffle_id = $1
                """,
                raffle_id,
                channel_id,
                message_id,
            )

    async def get_purchase_panel_ref(self, raffle_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raffle_id, purchase_panel_channel_id, purchase_panel_message_id
                FROM raffles
                WHERE raffle_id = $1
                """,
                raffle_id,
            )
            return dict(row) if row else None

    async def set_prize_image_url(self, raffle_id: int, url: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE raffles
                SET prize_image_url = $2
                WHERE raffle_id = $1
                """,
                raffle_id,
                url,
            )

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
                raffle = await conn.fetchrow(
                    """
                    SELECT raffle_id, status, tickets_available,
                           COALESCE((
                               SELECT SUM(num_tickets)
                               FROM raffle_entries
                               WHERE raffle_id = $1 AND payment_verified = TRUE
                           ), 0) AS verified_total
                    FROM raffles
                    WHERE raffle_id = $1
                    """,
                    raffle_id,
                )
                if not raffle:
                    return None

                if raffle["status"] != "active":
                    return None

                if int(raffle["verified_total"] or 0) < int(raffle["tickets_available"] or 0):
                    return None

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
