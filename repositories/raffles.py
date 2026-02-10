"""Raffle data access layer."""
from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Any

import asyncpg

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
            # Get creator's Torn ID for payment verification
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
                    end_time, end_trigger, hours_after_sold_out, status,
                    tickets_sold, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 'active', 0, NOW())
                RETURNING raffle_id
                """,
                guild_id,
                creator_discord_id,
                creator_torn_id,
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

    async def get_pending_verifications(self) -> list[dict[str, Any]]:
        """Get entries that need auto-verification (4:30+ minutes old, not verified)."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT 
                    re.entry_id, re.raffle_id, re.discord_id, re.torn_user_id, 
                    re.num_tickets, re.reserved_until, re.created_at,
                    r.creator_torn_id, r.ticket_price, r.ticket_payment_type,
                    u.api_key
                FROM raffle_entries re
                JOIN raffles r ON re.raffle_id = r.raffle_id
                JOIN user_api_keys u ON re.discord_id = u.discord_id
                WHERE re.payment_verified = FALSE
                AND re.reserved_until > NOW()
                AND re.created_at <= NOW() - INTERVAL '4 minutes 30 seconds'
                AND r.creator_torn_id IS NOT NULL
                """
            )
            return [dict(row) for row in rows]

    async def verify_payment_via_api(
        self, 
        entry_id: int,
        user_api_key: str,
        creator_torn_id: int,
        ticket_price: int,
        num_tickets: int,
        payment_type: str
    ) -> tuple[bool, Optional[str]]:
        """
        Verify payment by checking Torn API logs.
        Returns (success, error_message).
        """
        from utils.torn_api import get_torn_api, TornAPIError
        from cryptography.fernet import Fernet
        import config
        
        # Get item ID based on payment type
        item_id = config.XANAX_ITEM_ID if payment_type == "xanax" else config.DVD_ITEM_ID
        total_required = ticket_price * num_tickets
        
        try:
            # Decrypt API key
            f = Fernet(config.FERNET_KEY)
            api_key = f.decrypt(user_api_key.encode()).decode()
            
            # Get logs from last 10 minutes (to cover reservation period)
            from_timestamp = int(time.time()) - (10 * 60)
            
            api = get_torn_api()
            
            # Get item_sent logs (2210)
            logs = await api.get_user_logs(api_key, limit=50, log_types=[2210])
            
            for log_entry in logs:
                # Check timestamp is after reservation
                if log_entry.get("timestamp", 0) < from_timestamp:
                    continue
                
                # Check if sent to creator
                if str(log_entry.get("receiver_id")) != str(creator_torn_id):
                    continue
                
                # Check item type
                if str(log_entry.get("item_id")) != str(item_id):
                    continue
                
                # Check amount
                amount = log_entry.get("amount", 0)
                if amount >= total_required:
                    # Found valid payment!
                    return True, None
            
            return False, f"Payment not found. Need {total_required} {payment_type} sent to creator."
            
        except TornAPIError as e:
            return False, f"Torn API error: {e}"
        except Exception as e:
            return False, f"Verification error: {str(e)}"

    async def verify_payment_and_check_sold_out(
        self, entry_id: int, manual: bool = False
    ) -> tuple[bool, Optional[int], Optional[str]]:
        """
        Verify payment via Torn API and check if raffle sold out.
        Returns (success, raffle_id_if_sold_out, error_message).
        """
        async with self.pool.acquire() as conn:
            # Get entry details with raffle info
            entry = await conn.fetchrow(
                """
                SELECT 
                    re.*, r.creator_torn_id, r.ticket_price, 
                    r.ticket_payment_type, u.api_key
                FROM raffle_entries re
                JOIN raffles r ON re.raffle_id = r.raffle_id
                JOIN user_api_keys u ON re.discord_id = u.discord_id
                WHERE re.entry_id = $1
                AND re.payment_verified = FALSE
                """,
                entry_id
            )
            
            if not entry:
                return False, None, "Entry not found or already verified"
            
            # Check if reservation expired (unless manual verification)
            if not manual and entry["reserved_until"] < datetime.utcnow():
                return False, None, "Reservation expired"
            
            # Verify via Torn API
            success, error = await self.verify_payment_via_api(
                entry_id=entry_id,
                user_api_key=entry["api_key"],
                creator_torn_id=entry["creator_torn_id"],
                ticket_price=entry["ticket_price"],
                num_tickets=entry["num_tickets"],
                payment_type=entry["ticket_payment_type"]
            )
            
            if not success:
                return False, None, error
            
            # Payment verified! Update entry
            await conn.execute(
                """
                UPDATE raffle_entries 
                SET payment_verified = TRUE, 
                    payment_verified_at = NOW(),
                    reserved_until = NULL
                WHERE entry_id = $1
                """,
                entry_id,
            )
            
            raffle_id = entry["raffle_id"]
            
            # Check if this caused sell-out
            sold_out = await conn.fetchrow(
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
            
            return True, raffle_id if sold_out else None, None

    async def cancel_expired_reservation(self, entry_id: int) -> bool:
        """Cancel an expired reservation and return tickets to pool."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM raffle_entries 
                WHERE entry_id = $1 
                AND payment_verified = FALSE 
                AND reserved_until < NOW()
                """,
                entry_id
            )
            return "DELETE 1" in result

    async def draw_raffle_winner(
        self, raffle_id: int
    ) -> Optional[dict[str, Any]]:
        """Draw pure random winner (each entry has equal chance)."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
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
                
                # PURE RANDOM
                winner = random.choice(entries)
                winner_torn_name = f"User_{winner['torn_user_id']}"  # Simplified
                
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

    async def verify_prize_delivery(
        self, 
        raffle_id: int, 
        winner_discord_id: int,
        winner_torn_id: int,
        creator_torn_id: int,
    ) -> Optional[dict[str, Any]]:
        """Verify creator sent winner an item via Torn API logs using winner's API key."""
        from cryptography.fernet import Fernet
        import config
        
        async with self.pool.acquire() as conn:
            key_row = await conn.fetchrow(
                "SELECT api_key FROM user_api_keys WHERE discord_id = $1",
                winner_discord_id
            )
            
            if not key_row:
                return {"error": "Winner has no linked API key"}
            
            f = Fernet(config.FERNET_KEY)
            api_key = f.decrypt(key_row["api_key"].encode()).decode()
        
        from utils.torn_api import get_torn_api, TornAPIError
        api = get_torn_api()
        
        timestamp = int(time.time()) - (7 * 24 * 3600)
        
        try:
            logs = await api.get_user_logs(api_key, limit=200, log_types=[2211])
            
            for log_entry in logs:
                if log_entry.get("timestamp", 0) < timestamp:
                    continue
                
                if str(log_entry.get("sender_id")) == str(creator_torn_id):
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            """
                            UPDATE raffles 
                            SET status = 'completed',
                                prize_verified_at = NOW(),
                                verification_log_id = $1
                            WHERE raffle_id = $2
                            """,
                            str(log_entry.get("id") or log_entry.get("log_id")),
                            raffle_id,
                        )
                        
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
                            winner_discord_id,
                            raffle_id,
                            json.dumps({
                                "log_id": log_entry.get("id") or log_entry.get("log_id"),
                                "log_entry": log_entry,
                                "creator_torn_id": creator_torn_id,
                                "winner_torn_id": winner_torn_id,
                                "verified_at": datetime.utcnow().isoformat()
                            })
                        )
                    
                    return {
                        "verified": True,
                        "log_id": log_entry.get("id") or log_entry.get("log_id"),
                        "log_entry": log_entry,
                        "timestamp": log_entry.get("timestamp")
                    }
            
            return {"verified": False, "message": "No item received from creator found in logs"}
                    
        except TornAPIError as e:
            return {"verified": False, "error": str(e)}
        except Exception as e:
            return {"verified": False, "error": f"Verification failed: {str(e)}"}

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
                         reserved_until, payment_verified, created_at)
                    VALUES ($1, $2, $3, $4, $5, FALSE, NOW())
                    ON CONFLICT (raffle_id, discord_id) DO UPDATE
                    SET torn_user_id = EXCLUDED.torn_user_id,
                        num_tickets = EXCLUDED.num_tickets,
                        reserved_until = EXCLUDED.reserved_until,
                        payment_verified = FALSE,
                        payment_verified_at = NULL,
                        created_at = NOW()
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
                SELECT raffle_id, guild_id, creator_discord_id, creator_torn_id, prize,
                       ticket_payment_type, ticket_price, tickets_available,
                       max_tickets_per_user, status, winner_discord_id,
                       winner_torn_name, winner_torn_id, end_time, end_trigger, 
                       hours_after_sold_out, tickets_fully_sold_at, tickets_sold, 
                       created_at
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

    async def get_raffles_to_draw(self) -> list[dict[str, Any]]:
        """Get raffles ready to draw."""
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
                    (end_trigger = 'time' OR end_trigger IS NULL) 
                    AND end_time <= NOW()
                    OR
                    end_trigger = 'tickets_sold' 
                    AND tickets_fully_sold_at IS NOT NULL
                    AND tickets_fully_sold_at + (hours_after_sold_out || ' hours')::INTERVAL <= NOW()
                )
                """
            )
            return [dict(row) for row in rows]
