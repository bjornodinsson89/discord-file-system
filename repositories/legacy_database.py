"""
Database utilities for Happy Jumper Bot.
Complete implementation with all features aligned to production schema.
PgBouncer/Supabase pooler compatible.
"""

import asyncpg
import logging
import random
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import json
import config
from repositories.jumps import JumpsRepository
from repositories.raffles import RafflesRepository

log = logging.getLogger("happy_jumper.database")


class DatabaseManager:
    """Manages all database operations with PgBouncer compatibility."""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    @staticmethod
    def _pool_is_open(pool: Optional[asyncpg.Pool]) -> bool:
        """Return True when a pool exists and appears open.

        Uses public-ish API where possible and avoids relying on private
        implementation details like ``pool._closed``.
        """
        if pool is None:
            return False

        is_closing = getattr(pool, "is_closing", None)
        if callable(is_closing):
            return not bool(is_closing())

        # Fallback for unexpected asyncpg variants.
        closed = getattr(pool, "_closed", None)
        if closed is not None:
            return not bool(closed)

        return True
    
    async def init_pool(self) -> asyncpg.Pool:
        """Initialize connection pool."""
        if self._pool_is_open(self.pool):
            return self.pool
        ssl_mode = config.get_db_ssl_config()
        log.info("Initializing DB pool (ssl_mode=%s)", (config.DB_SSL or "disable").strip().lower())

        try:
            self.pool = await asyncpg.create_pool(
                host=config.DB_HOST,
                port=config.DB_PORT,
                database=config.DB_NAME,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                ssl=ssl_mode,
                min_size=2,
                max_size=10,
                command_timeout=60,
                # CRITICAL: Required for PgBouncer/Supabase pooler
                statement_cache_size=0,
            )
        except Exception:
            log.exception("Failed to initialize DB pool (ssl_mode=%s)", (config.DB_SSL or "disable").strip().lower())
            raise
        log.info("Database pool initialized with PgBouncer-safe settings")
        
        return self.pool


    async def close(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            log.info("Database pool closed")

    async def try_advisory_lock(self, lock_key: int) -> bool:
        """Try to acquire a PostgreSQL advisory lock for singleton tasks."""
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key))

    async def release_advisory_lock(self, lock_key: int) -> bool:
        """Release a PostgreSQL advisory lock if held."""
        async with self.pool.acquire() as conn:
            return bool(await conn.fetchval("SELECT pg_advisory_unlock($1)", lock_key))
    
    # ========================================================================
    # USER API KEYS
    # ========================================================================
    
    async def get_user_api_key(self, discord_id: int) -> Optional[Dict]:
        """Get user's API key data."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_api_keys WHERE discord_id = $1", discord_id
            )
            return dict(row) if row else None
    
    async def set_user_api_key(self, discord_id: int, torn_user_id: int, encrypted_key: str, guild_id: Optional[int] = None):
        """Store or update user's encrypted API key."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_api_keys (discord_id, torn_user_id, encrypted_key, guild_id)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (discord_id) DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    encrypted_key = EXCLUDED.encrypted_key,
                    guild_id = COALESCE(EXCLUDED.guild_id, user_api_keys.guild_id),
                    updated_at = NOW()
            """, discord_id, torn_user_id, encrypted_key, guild_id)
    
    async def delete_user_api_key(self, discord_id: int):
        """Delete user's API key."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM user_api_keys WHERE discord_id = $1", discord_id
            )
    
    async def get_all_user_api_keys(self) -> List[Dict]:
        """Get all stored API keys."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM user_api_keys")
            return [dict(row) for row in rows]

    async def get_user_api_keys_by_ids(self, discord_ids: List[int]) -> List[Dict]:
        """Get API key records for a list of Discord IDs."""
        if not discord_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM user_api_keys WHERE discord_id = ANY($1::bigint[])",
                discord_ids
            )
            return [dict(row) for row in rows]
    
    # ========================================================================
    # GUILD SETTINGS
    # ========================================================================
    
    async def create_or_update_guild_settings(self, guild_id: int) -> Dict:
        """Ensure guild settings row exists and return it."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO guild_settings (guild_id)
                VALUES ($1)
                ON CONFLICT (guild_id) DO UPDATE SET guild_id = EXCLUDED.guild_id
                RETURNING *
                """,
                guild_id,
            )
            return dict(row) if row else {}

    async def get_guild_settings(self, guild_id: int) -> Dict:
        """Get guild settings."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM guild_settings WHERE guild_id = $1",
                guild_id,
            )
            return dict(row) if row else {}
    
    async def update_guild_settings(self, guild_id: int, **kwargs):
        """Update guild settings and return updated row."""
        if not kwargs:
            return await self.get_guild_settings(guild_id)

        await self.create_or_update_guild_settings(guild_id)

        async with self.pool.acquire() as conn:
            sets = []
            values = []
            for i, (key, value) in enumerate(kwargs.items(), 1):
                sets.append(f"{key} = ${i}")
                values.append(value)
            values.append(guild_id)

            query = f"UPDATE guild_settings SET {', '.join(sets)} WHERE guild_id = ${len(values)} RETURNING *"
            row = await conn.fetchrow(query, *values)
            return dict(row) if row else {}

    @staticmethod
    def normalize_xanax_count(value: Optional[object]) -> int:
        if value is None:
            raise ValueError("Xanax count is required")

        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid xanax count. Allowed values: 1, 2, 3, 4.") from exc

        if normalized < 1 or normalized > 4:
            raise ValueError("Invalid xanax count. Allowed values: 1, 2, 3, 4.")

        return normalized

    @staticmethod
    def merge_raffle_tickets(existing_tickets: int, incoming_tickets: int, payment_verified: bool) -> int:
        """Overwrite unpaid reservations, accumulate paid ticket purchases."""
        if payment_verified:
            return int(existing_tickets) + int(incoming_tickets)
        return int(incoming_tickets)
    
    async def get_all_guild_settings(self) -> List[Dict]:
        """Get settings for all guilds."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM guild_settings")
            return [dict(row) for row in rows]
    
    # ========================================================================
    # AUDIT LOG
    # ========================================================================
    
    async def log_audit(
        self,
        actor_id: Optional[int],
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        payload: Optional[Dict] = None,
        guild_id: Optional[int] = None,
        source: str = 'discord'
    ):
        """Log an audit event."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO audit_log 
                (guild_id, actor_discord_id, action, target_type, target_id, payload, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, guild_id, actor_id, action, target_type, target_id,
                json.dumps(payload or {}), source)
    
    async def get_audit_logs(
        self,
        guild_id: Optional[int] = None,
        limit: int = 50,
        page: int = 1,
        actor_discord_id: Optional[int] = None,
        action: Optional[str] = None,
        target_type: Optional[str] = None,
        search: Optional[str] = None
    ) -> List[Dict]:
        """Get audit logs with filtering and pagination."""
        query = "SELECT * FROM audit_log WHERE 1=1"
        values = []
        idx = 1
        
        if guild_id:
            query += " AND guild_id = $" + str(idx)
            values.append(guild_id)
            idx += 1
        
        if actor_discord_id:
            query += " AND actor_discord_id = $" + str(idx)
            values.append(actor_discord_id)
            idx += 1
        
        if action:
            query += " AND action = $" + str(idx)
            values.append(action)
            idx += 1
        
        if target_type:
            query += " AND target_type = $" + str(idx)
            values.append(target_type)
            idx += 1

        if search:
            query += " AND (action ILIKE $" + str(idx) + " OR target_type ILIKE $" + str(idx) + " OR CAST(target_id AS TEXT) ILIKE $" + str(idx) + " OR CAST(actor_discord_id AS TEXT) ILIKE $" + str(idx) + ")"
            values.append(f"%{search}%")
            idx += 1
        
        offset = (page - 1) * limit
        query += " ORDER BY created_at DESC LIMIT $" + str(idx) + " OFFSET $" + str(idx + 1)
        values.extend([limit, offset])
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def get_audit_log_count(
        self,
        guild_id: Optional[int] = None,
        actor_discord_id: Optional[int] = None,
        action: Optional[str] = None,
        search: Optional[str] = None
    ) -> int:
        """Get total count of audit logs matching filters."""
        query = "SELECT COUNT(*) FROM audit_log WHERE 1=1"
        values = []
        idx = 1
        
        if guild_id:
            query += " AND guild_id = $" + str(idx)
            values.append(guild_id)
            idx += 1
        
        if actor_discord_id:
            query += " AND actor_discord_id = $" + str(idx)
            values.append(actor_discord_id)
            idx += 1
        
        if action:
            query += " AND action = $" + str(idx)
            values.append(action)
            idx += 1

        if search:
            query += " AND (action ILIKE $" + str(idx) + " OR target_type ILIKE $" + str(idx) + " OR CAST(target_id AS TEXT) ILIKE $" + str(idx) + " OR CAST(actor_discord_id AS TEXT) ILIKE $" + str(idx) + ")"
            values.append(f"%{search}%")
            idx += 1
        
        async with self.pool.acquire() as conn:
            try:
                return int(await conn.fetchval(query, *values) or 0)
            except asyncpg.PostgresError:
                log.warning("Audit log count query failed; returning 0", exc_info=True)
                return 0
    
    # ========================================================================
    # HAPPY JUMP SESSIONS
    # ========================================================================
    
    async def create_jump_session(
        self,
        guild_id: int,
        host_discord_id: int,
        host_torn_id: int,
        max_spots: int,
        xanax_count: int,
        start_in_hours: int,
        created_tct: int,
        estimated_jump_tct: int,
        payment_type: str,
        payment_amount: int,
        payment_item_id: Optional[int] = None
    ) -> int:
        """Create a new 99k jump session."""
        # Validate payment type
        if payment_type not in ('xanax', 'erotic_dvd'):
            raise ValueError(f"Invalid payment type: {payment_type}")
        
        xanax_count = self.normalize_xanax_count(xanax_count)

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO happy_jump_sessions (
                    guild_id, host_discord_id, host_torn_id, jump_type,
                    max_spots, xanax_count, start_in_hours,
                    created_tct, estimated_jump_tct,
                    payment_type, payment_amount, payment_item_id, status
                ) VALUES ($1, $2, $3, '99k', $4, $5, $6, $7, $8, $9, $10, $11, 'open')
                RETURNING id
            """, guild_id, host_discord_id, host_torn_id, max_spots, xanax_count,
                start_in_hours, created_tct, estimated_jump_tct,
                payment_type, payment_amount, payment_item_id)
            return row['id']
    
    async def get_jump_session(self, session_id: int) -> Optional[Dict]:
        """Get session by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM happy_jump_sessions WHERE id = $1", session_id
            )
            return dict(row) if row else None
    
    async def update_jump_session(self, session_id: int, **kwargs):
        """Update session fields using allowlisted parameterized statements."""
        allowed = {
            "status": "UPDATE happy_jump_sessions SET status = $2, updated_at = NOW() WHERE id = $1",
            "max_spots": "UPDATE happy_jump_sessions SET max_spots = $2, updated_at = NOW() WHERE id = $1",
            "xanax_count": "UPDATE happy_jump_sessions SET xanax_count = $2, updated_at = NOW() WHERE id = $1",
            "announcement_message_id": "UPDATE happy_jump_sessions SET announcement_message_id = $2, updated_at = NOW() WHERE id = $1",
            "completed_at": "UPDATE happy_jump_sessions SET completed_at = $2, updated_at = NOW() WHERE id = $1",
        }
        async with self.pool.acquire() as conn:
            for key, value in kwargs.items():
                query = allowed.get(key)
                if query:
                    await conn.execute(query, session_id, value)

    async def get_active_host_session(self, guild_id: int, host_discord_id: int) -> Optional[Dict]:
        """Get host's active session (for 1-active-per-host rule)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM happy_jump_sessions
                WHERE guild_id = $1 AND host_discord_id = $2 AND status IN ('open', 'locked')
                ORDER BY created_at DESC LIMIT 1
            """, guild_id, host_discord_id)
            return dict(row) if row else None
    
    async def has_active_session(self, host_discord_id: int) -> bool:
        """Check if host has any active session (guild-agnostic for global limit)."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM happy_jump_sessions
                WHERE host_discord_id = $1 AND status IN ('open', 'locked')
            """, host_discord_id)
            return count > 0
    
    async def get_active_sessions(self, guild_id: int) -> List[Dict]:
        """Get all active sessions for a guild."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM happy_jump_sessions
                WHERE guild_id = $1 AND status IN ('open', 'locked')
                ORDER BY created_at DESC
            """, guild_id)
            return [dict(row) for row in rows]
    
    async def get_session_history(
        self,
        guild_id: Optional[int] = None,
        host_discord_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 50,
        page: int = 1
    ) -> List[Dict]:
        """Get session history with filters and pagination."""
        query = "SELECT * FROM happy_jump_sessions WHERE 1=1"
        values = []
        idx = 1
        
        if guild_id:
            query += " AND guild_id = $" + str(idx)
            values.append(guild_id)
            idx += 1
        
        if host_discord_id:
            query += " AND host_discord_id = $" + str(idx)
            values.append(host_discord_id)
            idx += 1
        
        if status:
            query += " AND status = $" + str(idx)
            values.append(status)
            idx += 1
        
        offset = (page - 1) * limit
        query += " ORDER BY created_at DESC LIMIT $" + str(idx) + " OFFSET $" + str(idx + 1)
        values.extend([limit, offset])
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def get_session_count(
        self,
        guild_id: Optional[int] = None,
        status: Optional[str] = None
    ) -> int:
        """Get total count of sessions matching filters."""
        query = "SELECT COUNT(*) FROM happy_jump_sessions WHERE 1=1"
        values = []
        idx = 1
        
        if guild_id:
            query += " AND guild_id = $" + str(idx)
            values.append(guild_id)
            idx += 1
        
        if status:
            query += " AND status = $" + str(idx)
            values.append(status)
            idx += 1
        
        async with self.pool.acquire() as conn:
            try:
                return int(await conn.fetchval(query, *values) or 0)
            except asyncpg.PostgresError:
                log.warning("Audit log count query failed; returning 0", exc_info=True)
                return 0
    
    async def lock_session(self, session_id: int):
        """Lock a session to prevent new signups."""
        await self.update_jump_session(session_id, status='locked')
    
    async def cancel_session(self, session_id: int, reason: Optional[str] = None):
        """Cancel a session."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE happy_jump_sessions
                SET status = 'cancelled', updated_at = NOW()
                WHERE id = $1
            """, session_id)
    
    async def complete_session(self, session_id: int):
        """Mark session as completed."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE happy_jump_sessions
                SET status = 'completed', completed_at = NOW(), updated_at = NOW()
                WHERE id = $1
            """, session_id)
    
    # ========================================================================
    # JUMP SIGNUPS
    # ========================================================================
    
    async def create_signup(
        self,
        session_id: int,
        discord_id: int,
        torn_user_id: int,
        reserved_until: datetime
    ) -> int:
        """Create or refresh a signup reservation atomically."""
        repo = JumpsRepository(self.pool)
        return await repo.reserve_signup(session_id, discord_id, torn_user_id, reserved_until)
    
    async def get_session_signups(self, session_id: int) -> List[Dict]:
        """Get all signups for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM happy_jump_signups
                WHERE session_id = $1
                ORDER BY signed_up_at
            """, session_id)
            return [dict(row) for row in rows]
    
    async def get_confirmed_signups(self, session_id: int) -> List[Dict]:
        """Get confirmed signups for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM happy_jump_signups
                WHERE session_id = $1 AND status = 'confirmed'
                ORDER BY signed_up_at
            """, session_id)
            return [dict(row) for row in rows]
    
    async def get_signup(self, session_id: int, discord_id: int) -> Optional[Dict]:
        """Get specific signup."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM happy_jump_signups
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
            return dict(row) if row else None
    
    async def update_signup(self, session_id: int, discord_id: int, **kwargs):
        """Update signup fields using allowlisted parameterized statements."""
        allowed = {
            "status": "UPDATE happy_jump_signups SET status = $3 WHERE session_id = $1 AND discord_id = $2",
            "reserved_until": "UPDATE happy_jump_signups SET reserved_until = $3 WHERE session_id = $1 AND discord_id = $2",
            "payment_verified": "UPDATE happy_jump_signups SET payment_verified = $3 WHERE session_id = $1 AND discord_id = $2",
            "payment_verified_at": "UPDATE happy_jump_signups SET payment_verified_at = $3 WHERE session_id = $1 AND discord_id = $2",
        }
        async with self.pool.acquire() as conn:
            for key, value in kwargs.items():
                query = allowed.get(key)
                if query:
                    await conn.execute(query, session_id, discord_id, value)

    async def confirm_signup(self, session_id: int, discord_id: int):
        """Confirm a signup (payment verified)."""
        repo = JumpsRepository(self.pool)
        await repo.confirm_signup(session_id, discord_id)
    
    async def delete_signup(self, session_id: int, discord_id: int):
        """Delete a signup."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM happy_jump_signups
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
    
    async def cleanup_expired_signups(self) -> int:
        """Clean up expired signup reservations."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM happy_jump_signups
                WHERE status = 'reserved' AND reserved_until < NOW()
            """)
            deleted = int(result.split()[-1])
            if deleted > 0:
                log.info(f"Cleaned up {deleted} expired signup reservations")
            return deleted
    
    async def get_sessions_with_expired_signups(self) -> List[Dict]:
        """Get sessions that may have open spots due to expired signups."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT s.* FROM happy_jump_sessions s
                WHERE s.status = 'open'
                  AND s.max_spots > (
                      SELECT COUNT(*) FROM happy_jump_signups 
                      WHERE session_id = s.id AND status IN ('reserved', 'confirmed')
                  )
            """)
            return [dict(row) for row in rows]
    
    # ========================================================================
    # WAITLIST
    # ========================================================================
    
    async def add_to_waitlist(self, session_id: int, discord_id: int, torn_user_id: int) -> int:
        """Add user to session waitlist atomically. Returns position."""
        repo = JumpsRepository(self.pool)
        return await repo.add_to_waitlist(session_id, discord_id, torn_user_id)
    
    async def get_session_waitlist(self, session_id: int) -> List[Dict]:
        """Get waitlist for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM happy_jump_waitlist
                WHERE session_id = $1
                ORDER BY position
            """, session_id)
            return [dict(row) for row in rows]
    
    async def promote_from_waitlist(self, session_id: int) -> Optional[Dict]:
        """Promote first person from waitlist. Returns promoted user or None."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM happy_jump_waitlist
                WHERE session_id = $1
                ORDER BY position LIMIT 1
            """, session_id)
            
            if not row:
                return None
            
            user = dict(row)
            
            # Remove from waitlist
            await conn.execute("""
                DELETE FROM happy_jump_waitlist
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, user['discord_id'])
            
            # Reorder remaining
            await conn.execute("""
                UPDATE happy_jump_waitlist
                SET position = position - 1
                WHERE session_id = $1 AND position > $2
            """, session_id, user['position'])
            
            return user
    
    async def remove_from_waitlist(self, session_id: int, discord_id: int):
        """Remove user from waitlist."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT position FROM happy_jump_waitlist
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
            
            if row:
                await conn.execute("""
                    DELETE FROM happy_jump_waitlist
                    WHERE session_id = $1 AND discord_id = $2
                """, session_id, discord_id)
                
                await conn.execute("""
                    UPDATE happy_jump_waitlist
                    SET position = position - 1
                    WHERE session_id = $1 AND position > $2
                """, session_id, row['position'])
    
    async def get_waitlist_position(self, session_id: int, discord_id: int) -> Optional[int]:
        """Get user's position in waitlist."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT position FROM happy_jump_waitlist
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
    
    # ========================================================================
    # READINESS TRACKING
    # ========================================================================
    
    async def update_readiness(
        self,
        session_id: int,
        discord_id: int,
        energy: int,
        energy_max: int,
        drug_cooldown: int,
        status_text: str
    ):
        """Update user's readiness for a session."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO happy_jump_readiness
                (session_id, discord_id, energy, energy_max, drug_cooldown, status_text, checked_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (session_id, discord_id) DO UPDATE SET
                    energy = EXCLUDED.energy,
                    energy_max = EXCLUDED.energy_max,
                    drug_cooldown = EXCLUDED.drug_cooldown,
                    status_text = EXCLUDED.status_text,
                    checked_at = NOW()
            """, session_id, discord_id, energy, energy_max, drug_cooldown, status_text)
    
    async def get_session_readiness(self, session_id: int) -> List[Dict]:
        """Get readiness data for all participants in a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM happy_jump_readiness
                WHERE session_id = $1
            """, session_id)
            return [dict(row) for row in rows]
    
    # ========================================================================
    # HOST REPUTATION
    # ========================================================================
    
    async def get_host_reputation(self, discord_id: int) -> Optional[Dict]:
        """Get host's reputation stats."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM host_reputation WHERE discord_id = $1
            """, discord_id)
            return dict(row) if row else None

    async def get_host_reputation_by_ids(self, discord_ids: List[int]) -> List[Dict]:
        """Get host reputation entries for a list of Discord IDs."""
        if not discord_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM host_reputation WHERE discord_id = ANY($1::bigint[])",
                discord_ids
            )
            return [dict(row) for row in rows]
    
    async def update_host_reputation(self, discord_id: int, torn_id: int, completed: bool):
        """Update host reputation after session."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO host_reputation (discord_id, torn_id, sessions_completed, sessions_cancelled)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (discord_id) DO UPDATE SET
                    sessions_completed = host_reputation.sessions_completed + $3,
                    sessions_cancelled = host_reputation.sessions_cancelled + $4
            """, discord_id, torn_id, 1 if completed else 0, 0 if completed else 1)
    
    async def add_host_rating(
        self,
        host_discord_id: int,
        rater_discord_id: int,
        session_id: int,
        rating: int,
        comment: Optional[str] = None
    ):
        """Add a rating for a host."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO host_ratings (host_discord_id, rater_discord_id, session_id, rating, comment)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (host_discord_id, rater_discord_id, session_id) DO UPDATE SET
                    rating = EXCLUDED.rating,
                    comment = EXCLUDED.comment
            """, host_discord_id, rater_discord_id, session_id, rating, comment)
            
            # Update aggregate
            await conn.execute("""
                UPDATE host_reputation SET
                    average_rating = (
                        SELECT AVG(rating)::FLOAT FROM host_ratings WHERE host_discord_id = $1
                    ),
                    total_ratings = (
                        SELECT COUNT(*) FROM host_ratings WHERE host_discord_id = $1
                    ),
                    positive_ratings = (
                        SELECT COUNT(*) FROM host_ratings WHERE host_discord_id = $1 AND rating >= 4
                    ),
                    negative_ratings = (
                        SELECT COUNT(*) FROM host_ratings WHERE host_discord_id = $1 AND rating <= 2
                    )
                WHERE discord_id = $1
            """, host_discord_id)
    
    # ========================================================================
    # BLACKLIST
    # ========================================================================
    
    async def add_to_blacklist(
        self,
        guild_id: int,
        discord_id: int,
        reason: str,
        banned_by: int,
        expires_at: Optional[datetime] = None
    ):
        """Add user to blacklist."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO blacklist (guild_id, discord_id, reason, banned_by, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (guild_id, discord_id) DO UPDATE SET
                    reason = EXCLUDED.reason,
                    banned_by = EXCLUDED.banned_by,
                    expires_at = EXCLUDED.expires_at,
                    created_at = NOW()
            """, guild_id, discord_id, reason, banned_by, expires_at)
    
    async def remove_from_blacklist(self, guild_id: int, discord_id: int):
        """Remove user from blacklist."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM blacklist WHERE guild_id = $1 AND discord_id = $2
            """, guild_id, discord_id)
    
    async def is_blacklisted(self, guild_id: int, discord_id: int) -> Optional[Dict]:
        """Check if user is blacklisted and return active entry."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM blacklist
                WHERE guild_id = $1 AND discord_id = $2
                  AND (expires_at IS NULL OR expires_at > NOW())
            """, guild_id, discord_id)
            return dict(row) if row else None
    
    async def get_blacklist(self, guild_id: int) -> List[Dict]:
        """Get all blacklisted users for a guild."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM blacklist
                WHERE guild_id = $1 AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC
            """, guild_id)
            return [dict(row) for row in rows]
    
    # ========================================================================
    # INSURANCE PROVIDERS
    # ========================================================================
    
    async def create_provider(
        self,
        discord_id: int,
        torn_user_id: int,
        company_name: Optional[str] = None
    ) -> int:
        """Create or update insurance provider."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO insurance_providers (discord_id, torn_user_id, company_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (discord_id) DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    company_name = COALESCE(EXCLUDED.company_name, insurance_providers.company_name)
                RETURNING provider_id
            """, discord_id, torn_user_id, company_name)
            return row['provider_id']
    
    async def get_provider(self, discord_id: int) -> Optional[Dict]:
        """Get provider by Discord ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM insurance_providers WHERE discord_id = $1
            """, discord_id)
            return dict(row) if row else None
    
    async def get_provider_by_id(self, provider_id: int) -> Optional[Dict]:
        """Get provider by provider ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM insurance_providers WHERE provider_id = $1
            """, provider_id)
            return dict(row) if row else None

    async def get_providers_by_discord_ids(self, discord_ids: List[int]) -> List[Dict]:
        """Get insurance providers for a list of Discord IDs."""
        if not discord_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM insurance_providers WHERE discord_id = ANY($1::bigint[])",
                discord_ids
            )
            return [dict(row) for row in rows]

    async def get_member_activity_stats(self, guild_id: int, discord_ids: List[int]) -> Dict[int, Dict[str, int]]:
        """Get per-member activity stats for a guild."""
        if not discord_ids:
            return {}

        async with self.pool.acquire() as conn:
            joined_rows = await conn.fetch("""
                SELECT s.discord_id, COUNT(*)::int AS sessions_joined
                FROM happy_jump_signups s
                JOIN happy_jump_sessions hs ON hs.id = s.session_id
                WHERE hs.guild_id = $1 AND s.discord_id = ANY($2::bigint[])
                GROUP BY s.discord_id
            """, guild_id, discord_ids)

            hosted_rows = await conn.fetch("""
                SELECT host_discord_id AS discord_id, COUNT(*)::int AS sessions_hosted
                FROM happy_jump_sessions
                WHERE guild_id = $1 AND host_discord_id = ANY($2::bigint[])
                GROUP BY host_discord_id
            """, guild_id, discord_ids)

        stats: Dict[int, Dict[str, int]] = {}
        for row in joined_rows:
            stats[int(row["discord_id"])] = {"sessions_joined": row["sessions_joined"], "sessions_hosted": 0}
        for row in hosted_rows:
            discord_id = int(row["discord_id"])
            if discord_id not in stats:
                stats[discord_id] = {"sessions_joined": 0, "sessions_hosted": row["sessions_hosted"]}
            else:
                stats[discord_id]["sessions_hosted"] = row["sessions_hosted"]
        return stats
    

    async def get_registered_users_for_guild(self, guild_id: int) -> List[Dict]:
        """Get registered users for a guild based on DB activity and API key registration."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                WITH relevant_users AS (
                    SELECT host_discord_id AS discord_id
                    FROM happy_jump_sessions
                    WHERE guild_id = $1
                    UNION
                    SELECT s.discord_id
                    FROM happy_jump_signups s
                    JOIN happy_jump_sessions hs ON hs.id = s.session_id
                    WHERE hs.guild_id = $1
                    UNION
                    SELECT uak.discord_id
                    FROM user_api_keys uak
                    WHERE uak.guild_id = $1
                )
                SELECT
                    ru.discord_id,
                    uak.torn_user_id,
                    (uak.discord_id IS NOT NULL) AS has_api_key,
                    (hr.discord_id IS NOT NULL) AS is_host,
                    (ip.discord_id IS NOT NULL AND ip.approval_status = 'approved') AS is_insurer,
                    COALESCE(joined.sessions_joined, 0)::int AS sessions_joined,
                    COALESCE(hosted.sessions_hosted, 0)::int AS sessions_hosted,
                    uak.created_at
                FROM relevant_users ru
                LEFT JOIN user_api_keys uak ON uak.discord_id = ru.discord_id
                LEFT JOIN host_reputation hr ON hr.discord_id = ru.discord_id
                LEFT JOIN insurance_providers ip ON ip.discord_id = ru.discord_id
                LEFT JOIN (
                    SELECT s.discord_id, COUNT(*)::int AS sessions_joined
                    FROM happy_jump_signups s
                    JOIN happy_jump_sessions hs ON hs.id = s.session_id
                    WHERE hs.guild_id = $1
                    GROUP BY s.discord_id
                ) joined ON joined.discord_id = ru.discord_id
                LEFT JOIN (
                    SELECT host_discord_id AS discord_id, COUNT(*)::int AS sessions_hosted
                    FROM happy_jump_sessions
                    WHERE guild_id = $1
                    GROUP BY host_discord_id
                ) hosted ON hosted.discord_id = ru.discord_id
                ORDER BY ru.discord_id
            """, guild_id)
            return [dict(row) for row in rows]
    async def approve_provider(self, provider_id: int, approved_by: int):
        """Approve a provider."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_providers
                SET approval_status = 'approved', verified = TRUE, active = TRUE,
                    approved_by = $2, approved_at = NOW()
                WHERE provider_id = $1
            """, provider_id, approved_by)
    
    async def reject_provider(self, provider_id: int, approved_by: int):
        """Reject a provider."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_providers
                SET approval_status = 'rejected', approved_by = $2, approved_at = NOW()
                WHERE provider_id = $1
            """, provider_id, approved_by)
    
    async def set_provider_active(self, provider_id: int, active: bool):
        """Enable/disable provider."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_providers SET active = $2 WHERE provider_id = $1
            """, provider_id, active)
    
    async def get_active_providers(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Get all active approved providers."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM insurance_providers
                WHERE approval_status = 'approved' AND active = TRUE
                ORDER BY created_at
            """)
            return [dict(row) for row in rows]

    async def get_approved_providers_for_browser(
        self,
        guild_id: int,
        active_only: bool = True,
        coverage_type: Optional[str] = None,
        jump_type: Optional[str] = "99k",
    ) -> List[Dict]:
        """Get approved providers for guild browser list with policy summary."""
        async with self.pool.acquire() as conn:
            provider_rows = await conn.fetch(
                """
                SELECT provider_id, discord_id, torn_user_id, company_name, application_data
                FROM insurance_providers
                WHERE guild_id = $1
                  AND approval_status = 'approved'
                  AND (active = TRUE OR $2 = FALSE)
                ORDER BY provider_id DESC
                """,
                guild_id,
                active_only,
            )

            providers = [dict(row) for row in provider_rows]
            if not providers:
                return []

            provider_ids = [int(row["provider_id"]) for row in providers]

            summary_query = """
                SELECT provider_id,
                       array_agg(DISTINCT coverage_type) AS types,
                       COUNT(*) FILTER (WHERE active = TRUE) AS active_policy_count,
                       COUNT(*) AS total_policy_count
                FROM insurance_policies
                WHERE guild_id = $1
                  AND provider_id = ANY($2::int[])
            """
            summary_params: List = [guild_id, provider_ids]
            idx = 3

            if coverage_type:
                summary_query += " AND coverage_type = $" + str(idx)
                summary_params.append(coverage_type)
                idx += 1

            if jump_type:
                summary_query += " AND $" + str(idx) + " = ANY(covered_jump_types)"
                summary_params.append(jump_type)

            summary_query += " GROUP BY provider_id"
            summary_rows = await conn.fetch(summary_query, *summary_params)
            summary_by_provider = {
                int(row["provider_id"]): {
                    "types": [t for t in (row["types"] or []) if t],
                    "active_policy_count": int(row["active_policy_count"] or 0),
                    "total_policy_count": int(row["total_policy_count"] or 0),
                }
                for row in summary_rows
            }

            filtered: List[Dict] = []
            for provider in providers:
                provider_id = int(provider["provider_id"])
                summary = summary_by_provider.get(provider_id)
                if (coverage_type or jump_type) and not summary:
                    continue
                provider["policy_types"] = (summary or {}).get("types", [])
                provider["active_policy_count"] = (summary or {}).get("active_policy_count", 0)
                provider["total_policy_count"] = (summary or {}).get("total_policy_count", 0)
                filtered.append(provider)

            return filtered

    async def get_provider_policies_for_browser(
        self,
        guild_id: int,
        provider_id: int,
        active_only: bool = True,
        coverage_type: Optional[str] = None,
        jump_type: Optional[str] = "99k",
    ) -> List[Dict]:
        """Get provider policies for insurer card view."""
        query = """
            SELECT *
            FROM insurance_policies
            WHERE guild_id = $1
              AND provider_id = $2
              AND (active = TRUE OR $3 = FALSE)
        """
        params: List = [guild_id, provider_id, active_only]
        idx = 4

        if coverage_type:
            query += " AND coverage_type = $" + str(idx)
            params.append(coverage_type)
            idx += 1

        if jump_type:
            query += " AND $" + str(idx) + " = ANY(covered_jump_types)"
            params.append(jump_type)

        query += " ORDER BY policy_id DESC"

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [dict(row) for row in rows]
    
    async def get_all_providers(self, approval_status: Optional[str] = None) -> List[Dict]:
        """Get all providers with optional status filter."""
        query = "SELECT * FROM insurance_providers"
        values = []
        
        if approval_status:
            query += " WHERE approval_status = $1"
            values.append(approval_status)
        
        query += " ORDER BY created_at DESC"
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def upsert_insurer_application(
        self,
        guild_id: int,
        discord_id: int,
        torn_user_id: int,
        company_name: Optional[str],
        application_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create or update pending insurer application by discord_id."""
        has_guild_id = await self.column_exists("insurance_providers", "guild_id")
        has_application_data = await self.column_exists("insurance_providers", "application_data")

        async with self.pool.acquire() as conn:
            if has_guild_id and has_application_data:
                row = await conn.fetchrow("""
                    INSERT INTO insurance_providers
                        (discord_id, torn_user_id, guild_id, company_name, application_data, approval_status, verified, active, denial_reason, approved_by, approved_at)
                    VALUES
                        ($1, $2, $3, $4, $5::jsonb, 'pending', FALSE, FALSE, NULL, NULL, NULL)
                    ON CONFLICT (discord_id) DO UPDATE SET
                        torn_user_id = EXCLUDED.torn_user_id,
                        guild_id = EXCLUDED.guild_id,
                        company_name = COALESCE(EXCLUDED.company_name, insurance_providers.company_name),
                        application_data = EXCLUDED.application_data,
                        approval_status = 'pending',
                        verified = FALSE,
                        active = FALSE,
                        denial_reason = NULL,
                        approved_by = NULL,
                        approved_at = NULL
                    RETURNING *
                """, discord_id, torn_user_id, guild_id, company_name, json.dumps(application_data))
            elif has_application_data:
                row = await conn.fetchrow("""
                    INSERT INTO insurance_providers
                        (discord_id, torn_user_id, company_name, application_data, approval_status, verified, active, denial_reason, approved_by, approved_at)
                    VALUES
                        ($1, $2, $3, $4::jsonb, 'pending', FALSE, FALSE, NULL, NULL, NULL)
                    ON CONFLICT (discord_id) DO UPDATE SET
                        torn_user_id = EXCLUDED.torn_user_id,
                        company_name = COALESCE(EXCLUDED.company_name, insurance_providers.company_name),
                        application_data = EXCLUDED.application_data,
                        approval_status = 'pending',
                        verified = FALSE,
                        active = FALSE,
                        denial_reason = NULL,
                        approved_by = NULL,
                        approved_at = NULL
                    RETURNING *
                """, discord_id, torn_user_id, company_name, json.dumps(application_data))
            elif has_guild_id:
                row = await conn.fetchrow("""
                    INSERT INTO insurance_providers
                        (discord_id, torn_user_id, guild_id, company_name, approval_status, verified, active, approved_by, approved_at)
                    VALUES
                        ($1, $2, $3, $4, 'pending', FALSE, FALSE, NULL, NULL)
                    ON CONFLICT (discord_id) DO UPDATE SET
                        torn_user_id = EXCLUDED.torn_user_id,
                        guild_id = EXCLUDED.guild_id,
                        company_name = COALESCE(EXCLUDED.company_name, insurance_providers.company_name),
                        approval_status = 'pending',
                        verified = FALSE,
                        active = FALSE,
                        approved_by = NULL,
                        approved_at = NULL
                    RETURNING *
                """, discord_id, torn_user_id, guild_id, company_name)
            else:
                row = await conn.fetchrow("""
                    INSERT INTO insurance_providers
                        (discord_id, torn_user_id, company_name, approval_status, verified, active, approved_by, approved_at)
                    VALUES
                        ($1, $2, $3, 'pending', FALSE, FALSE, NULL, NULL)
                    ON CONFLICT (discord_id) DO UPDATE SET
                        torn_user_id = EXCLUDED.torn_user_id,
                        company_name = COALESCE(EXCLUDED.company_name, insurance_providers.company_name),
                        approval_status = 'pending',
                        verified = FALSE,
                        active = FALSE,
                        approved_by = NULL,
                        approved_at = NULL
                    RETURNING *
                """, discord_id, torn_user_id, company_name)

        provider = dict(row)
        if not has_application_data:
            payload = dict(application_data)
            payload["provider_id"] = provider["provider_id"]
            await self.log_audit(
                actor_id=discord_id,
                action="insurer_application_submitted",
                target_type="insurance_provider",
                target_id=provider["provider_id"],
                payload=payload,
                guild_id=guild_id,
                source="discord",
            )
        return provider

    async def upsert_host_application(
        self,
        guild_id: int,
        discord_id: int,
        torn_user_id: int,
        torn_name: Optional[str],
        display_name: Optional[str],
        forum_url: str,
        application_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create or update pending host application by guild/user."""
        normalized_torn_name = (torn_name or "").strip()
        normalized_display_name = (display_name or "").strip() or None

        if not normalized_torn_name:
            fallback_name = (normalized_display_name or "").strip()
            if fallback_name:
                normalized_torn_name = fallback_name

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO host_applications
                    (guild_id, discord_id, torn_user_id, torn_name, display_name, forum_url, application_data, approval_status, approved_by, approved_at, denial_reason)
                VALUES
                    ($1, $2, $3, $4, $5, $6, $7::jsonb, 'pending', NULL, NULL, NULL)
                ON CONFLICT (guild_id, discord_id) DO UPDATE SET
                    torn_user_id = EXCLUDED.torn_user_id,
                    torn_name = EXCLUDED.torn_name,
                    display_name = EXCLUDED.display_name,
                    forum_url = EXCLUDED.forum_url,
                    application_data = EXCLUDED.application_data,
                    approval_status = 'pending',
                    approved_by = NULL,
                    approved_at = NULL,
                    denial_reason = NULL
                RETURNING *
            """, guild_id, discord_id, torn_user_id, normalized_torn_name, normalized_display_name, forum_url, json.dumps(application_data))
        return dict(row)

    async def get_host_application_by_id(self, application_id: int) -> Optional[Dict]:
        """Get host application by id."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM host_applications WHERE id = $1", application_id)
            return dict(row) if row else None


    async def list_pending_insurer_applications(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """List pending insurer applications for persistent review views."""
        query = "SELECT provider_id, discord_id, guild_id FROM insurance_providers WHERE approval_status = 'pending'"
        values = []
        if guild_id is not None:
            query += " AND guild_id = $1"
            values.append(guild_id)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *values)
        return [dict(row) for row in rows]

    async def list_pending_host_applications(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """List pending host99k applications for persistent review views."""
        query = "SELECT id, discord_id, guild_id FROM host_applications WHERE approval_status = 'pending'"
        values = []
        if guild_id is not None:
            query += " AND guild_id = $1"
            values.append(guild_id)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *values)
        return [dict(row) for row in rows]

    async def review_insurer_application(
        self,
        provider_id: int,
        decision: str,
        admin_discord_id: int,
        reason: Optional[str] = None,
    ) -> Optional[Dict]:
        """Approve/deny insurer application."""
        async with self.pool.acquire() as conn:
            if decision == 'approve':
                query = """
                    UPDATE insurance_providers
                    SET approval_status = 'approved', verified = TRUE, active = TRUE,
                        approved_by = $2, approved_at = NOW(), denial_reason = NULL
                    WHERE provider_id = $1
                    RETURNING *
                """
                row = await conn.fetchrow(query, provider_id, admin_discord_id)
            else:
                reason_payload = json.dumps({"review_reason": reason})
                row = await conn.fetchrow("""
                    UPDATE insurance_providers
                    SET approval_status = 'rejected', verified = FALSE, active = FALSE,
                        approved_by = $2, approved_at = NOW(), denial_reason = $3::text,
                        application_data = COALESCE(application_data, '{}'::jsonb) || $4::jsonb
                    WHERE provider_id = $1
                    RETURNING *
                """, provider_id, admin_discord_id, reason, reason_payload)


        if row:
            return dict(row)
        return None


# ============================================================================
# MODULE-LEVEL SINGLETON
# ============================================================================

_db_manager: Optional[DatabaseManager] = None
_db_loop: Optional[asyncio.AbstractEventLoop] = None
_db_lock: Optional[asyncio.Lock] = None


async def init_database() -> DatabaseManager:
    """Initialize the database manager singleton."""
    global _db_manager, _db_loop, _db_lock
    current_loop = asyncio.get_running_loop()

    if _db_lock is None:
        _db_lock = asyncio.Lock()

    async with _db_lock:
        if _db_manager and _db_loop is current_loop:
            return _db_manager

        if _db_manager and _db_loop is not current_loop:
            log.warning("Database manager initialized on a different event loop; recreating pool")
            try:
                await _db_manager.close()
            except Exception:
                log.exception("Failed closing previous database pool")

        _db_manager = DatabaseManager()
        await _db_manager.init_pool()
        _db_loop = current_loop
        return _db_manager


def get_database() -> DatabaseManager:
    """Get the database manager singleton."""
    if _db_manager is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db_manager
