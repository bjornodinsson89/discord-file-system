"""Raffle data access layer."""
from __future__ import annotations

import json
import random
import time
from datetime import datetime
from typing import Optional, Any

import asyncpg
import aiohttp

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

    async def draw_raffle_winner(
        self, raffle_id: int
    ) -> Optional[dict[str, Any]]:
        """Draw pure random winner (each entry has equal chance)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Get raffle details including creator info
                raffle = await conn.fetchrow(
                    """
                    SELECT r.*, u.torn_user_id as creator_torn_id
                    FROM raffles r
                    LEFT JOIN user_api_keys u ON r.creator_discord_id = u.discord_id
                    WHERE r.raffle_id = $1
                    """,
                    raffle_id,
                )
                
                if not raffle:
                    return None
                
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
                
                # PURE RANDOM: Each user has equal chance regardless of ticket count
                winner = random.choice(entries)
                
                # Get winner's Torn name
                winner_torn_name = await self._get_torn_name(winner["torn_user_id"])
                
                # Store winner info in raffle record
                await conn.execute(
                    """
                    UPDATE raffles 
                    SET winner_discord_id = $1,
                        winner_torn_id = $2,
                        winner_torn_name = $3,
                        winning_entry_id = $4,
                        status = 'awaiting_delivery',
                        drawn_at = NOW()
                    WHERE raffle_id = $5
                    """,
                    winner["discord_id"],
                    winner["torn_user_id"],
                    winner_torn_name,
                    winner["entry_id"],
                    raffle_id,
                )
                
                return {
                    "raffle_id": raffle_id,
                    "discord_id": winner["discord_id"],
                    "torn_user_id": winner["torn_user_id"],
                    "torn_name": winner_torn_name,
                    "creator_discord_id": raffle["creator_discord_id"],
                    "creator_torn_id": raffle["creator_torn_id"],
                    "prize": raffle["prize"],
                    "total_entries": len(entries),
                }
    
    async def _get_torn_name(self, torn_id: int) -> str:
        """Fetch Torn name from API."""
        try:
            async with aiohttp.ClientSession() as session:
                # Note: You need to add TORN_API_KEY to config
                from config import TORN_API_KEY
                async with session.get(
                    f"https://api.torn.com/user/{torn_id}?selections=basic&key={TORN_API_KEY}"
                ) as resp:
                    data = await resp.json()
                    return data.get("name", f"User_{torn_id}")
        except Exception:
            return f"User_{torn_id}"
    
    async def verify_prize_delivery(
        self, 
        raffle_id: int, 
        winner_torn_id: int,
        creator_torn_id: int,
        api_key: str
    ) -> Optional[dict[str, Any]]:
        """Verify creator sent winner an item via Torn API logs."""
        # Check logs from last 7 days
        timestamp = int(time.time()) - (7 * 24 * 3600)
        
        try:
            async with aiohttp.ClientSession() as session:
                url = (
                    f"https://api.torn.com/user/{winner_torn_id}"
                    f"?selections=log&log=2211&from={timestamp}&key={api_key}"
                )
                async with session.get(url) as resp:
                    data = await resp.json()
                    
                    if "error" in data:
                        return {"error": data["error"]["error"]}
                    
                    logs = data.get("log", {})
                    
                    # Look for item_received log where sender is creator
                    for log_id, log_entry in logs.items():
                        if str(log_entry.get("sender_id")) == str(creator_torn_id):
                            # Found it! Mark as verified
                            async with self.pool.acquire() as conn:
                                await conn.execute(
                                    """
                                    UPDATE raffles 
                                    SET status = 'completed',
                                        prize_verified_at = NOW(),
                                        verification_log_id = $1
                                    WHERE raffle_id = $2
                                    """,
                                    log_id,
                                    raffle_id,
                                )
                                
                                # Log to audit
                                await conn.execute(
                                    """
                                    INSERT INTO audit_log (
                                        actor_discord_id, action, target_type, 
                                        target_id, payload, guild_id, source, created_at
                                    ) VALUES (
                                        $1, 'prize_verified', 'raffle', $2, $3, 
                                        (SELECT guild_id FROM raffles WHERE raffle_id = $2),
                                        'raffle_system', NOW()
                                    )
                                    """,
                                    winner_torn_id,
                                    raffle_id,
                                    json.dumps({
                                        "log_id": log_id,
                                        "log_entry": log_entry,
                                        "creator_torn_id": creator_torn_id,
                                        "winner_torn_id": winner_torn_id,
                                        "verified_at": datetime.utcnow().isoformat()
                                    })
                                )
                            
                            return {
                                "verified": True,
                                "log_id": log_id,
                                "log_entry": log_entry,
                                "timestamp": log_entry.get("timestamp")
                            }
                    
                    return {"verified": False, "message": "No item received from creator found in logs"}
                    
        except Exception as e:
            return {"verified": False, "error": str(e)}

    async def reserve_entry(
        self, raffle_id: int, discord_id: int, torn_user_id: int,
        num_tickets: int, reserved_until: datetime
    ) -> Optional[dict]:
        """Reserve entry and check if sold out."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
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
                
                # Check if this completed the raffle
                if row:
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

    async def verify_payment_and_check_sold_out(
        self, entry_id: int
    ) -> tuple[bool, Optional[int]]:
        """Verify payment and return (success, raffle_id_if_sold_out)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
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

    async def get_raffle(self, raffle_id: int) -> Optional[dict[str, Any]]:
        """Get single raffle by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT raffle_id, guild_id, creator_discord_id, prize,
                       ticket_payment_type, ticket_price, tickets_available,
                       max_tickets_per_user, status, winner_discord_id,
                       winner_torn_name, end_time, end_trigger, hours_after_sold_out,
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
            
            query = f"UPDATE raffles SET {', '.join(join(sets))} WHERE raffle_id = ${len(values)}"
            result = await conn.execute(query, *values)
            return "UPDATE 1" in result

    async def cleanup_expired_raffle_entries(self) -> int:
        """Clean up expired unpaid reservations."""
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
        """Get active raffles."""
        async with self.pool.acquire() as conn:
            if guild_id:
                rows = await conn.fetch(
                    """
                    SELECT raffle_id, guild_id, creator_discord_id, prize, status,
                           end_time, tickets_sold, tickets_available, end_trigger,
                           hours_after_sold_out, tickets_fully_sold_at
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
                           end_time, tickets_sold, tickets_available, end_trigger,
                           hours_after_sold_out, tickets_fully_sold_at
                    FROM raffles
                    WHERE status = 'active'
                    """
                )
            return [dict(row) for row in rows]
