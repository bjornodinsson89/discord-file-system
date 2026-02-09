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
    
    async def init_pool(self, run_migrations: bool = False) -> asyncpg.Pool:
        """Initialize connection pool with PgBouncer-safe settings."""
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
        
        # Apply emergency schema fixes only when explicitly enabled.
        if config.RUN_EMERGENCY_SCHEMA_FIXES:
            await self.apply_emergency_schema_fixes()
        else:
            log.info("Skipping emergency schema fixes (RUN_EMERGENCY_SCHEMA_FIXES is disabled)")
        
        log.info(
            "Migration gate: RUN_MIGRATIONS=%s run_migrations=%s",
            config.RUN_MIGRATIONS,
            run_migrations,
        )

        if run_migrations:
            from migrations.migration_runner import run_migrations as run_pending_migrations
            try:
                applied_count = await run_pending_migrations(self.pool)
                if applied_count > 0:
                    log.info(f"Applied {applied_count} database migrations")
                else:
                    log.info("No pending migrations applied")
            except Exception:
                log.exception("Database migrations failed")
                raise
        else:
            log.info("Skipping migrations on startup")

        return self.pool
    
    async def apply_emergency_schema_fixes(self):
        """Apply emergency schema fixes for missing columns (PgBouncer-safe)."""
        try:
            async with self.pool.acquire() as conn:
                # Fix 1: Add audit_log.source column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name = 'audit_log' AND column_name = 'source'
                        ) THEN
                            ALTER TABLE audit_log ADD COLUMN source VARCHAR(20) DEFAULT 'discord';
                        END IF;
                    END $$;
                """)
                log.info("✅ audit_log.source column verified")
                
                # Fix 2: Add insurance_coverage.reserved_until column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name = 'insurance_coverage' AND column_name = 'reserved_until'
                        ) THEN
                            ALTER TABLE insurance_coverage ADD COLUMN reserved_until TIMESTAMPTZ;
                        END IF;
                    END $$;
                """)
                log.info("✅ insurance_coverage.reserved_until column verified")
                
                # Fix 3: Add insurance_coverage.last_log_timestamp column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name = 'insurance_coverage' AND column_name = 'last_log_timestamp'
                        ) THEN
                            ALTER TABLE insurance_coverage ADD COLUMN last_log_timestamp BIGINT;
                        END IF;
                    END $$;
                """)
                log.info("✅ insurance_coverage.last_log_timestamp column verified")

                # Fix 4: Add happy_jump_signups.reserved_until column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'happy_jump_signups'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'happy_jump_signups' AND column_name = 'reserved_until'
                        ) THEN
                            ALTER TABLE happy_jump_signups ADD COLUMN reserved_until TIMESTAMPTZ;
                        END IF;
                    END $$;
                """)
                log.info("✅ happy_jump_signups.reserved_until column verified")

                # Fix 5: Add raffle_entries.reserved_until column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'raffle_entries'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'raffle_entries' AND column_name = 'reserved_until'
                        ) THEN
                            ALTER TABLE raffle_entries ADD COLUMN reserved_until TIMESTAMPTZ;
                        END IF;
                    END $$;
                """)
                log.info("✅ raffle_entries.reserved_until column verified")
                
                # Fix 6: Fix raffle status constraint
                await conn.execute("""
                    DO $$
                    BEGIN
                        -- Drop old constraint if exists
                        ALTER TABLE raffles DROP CONSTRAINT IF EXISTS chk_raffle_status;
                        
                        -- Add new constraint with 'drawing' status
                        ALTER TABLE raffles ADD CONSTRAINT chk_raffle_status 
                        CHECK (status IN ('active', 'drawing', 'completed', 'cancelled'));
                    END $$;
                """)
                log.info("✅ raffles status constraint fixed")
                

                # Fix 7: Add user_api_keys.guild_id column
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'user_api_keys'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'user_api_keys' AND column_name = 'guild_id'
                        ) THEN
                            ALTER TABLE user_api_keys ADD COLUMN guild_id BIGINT;
                        END IF;
                    END $$;
                """)
                log.info("✅ user_api_keys.guild_id column verified")

                # Fix 8: Add welcome settings columns
                await conn.execute("""
                    DO $$
                    BEGIN
                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'guild_settings'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'guild_settings' AND column_name = 'welcome_channel_id'
                        ) THEN
                            ALTER TABLE guild_settings ADD COLUMN welcome_channel_id BIGINT;
                        END IF;

                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'guild_settings'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'guild_settings' AND column_name = 'welcome_message_template'
                        ) THEN
                            ALTER TABLE guild_settings ADD COLUMN welcome_message_template TEXT;
                        END IF;

                        IF EXISTS (
                            SELECT 1 FROM information_schema.tables
                            WHERE table_name = 'guild_settings'
                        ) AND NOT EXISTS (
                            SELECT 1 FROM information_schema.columns
                            WHERE table_name = 'guild_settings' AND column_name = 'welcome_enabled'
                        ) THEN
                            ALTER TABLE guild_settings ADD COLUMN welcome_enabled BOOLEAN DEFAULT FALSE;
                        END IF;
                    END $$;
                """)
                log.info("✅ guild_settings welcome columns verified")
                
                log.info("🎉 All emergency schema fixes applied!")
                
        except Exception as e:
            log.error(f"❌ Emergency schema fix failed: {e}")
            # Don't re-raise - let the bot continue
    
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
    
    async def get_guild_settings(self, guild_id: int) -> Dict:
        """Get guild settings, creating default entry if needed."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
                guild_id
            )
            row = await conn.fetchrow(
                "SELECT * FROM guild_settings WHERE guild_id = $1", guild_id
            )
            return dict(row)
    
    async def update_guild_settings(self, guild_id: int, **kwargs):
        """Update guild settings."""
        if not kwargs:
            return
        
        # Build dynamic SET clause
        sets = []
        values = [guild_id]
        for i, (key, value) in enumerate(kwargs.items(), start=2):
            sets.append(f"{key} = ${i}")
            values.append(value)
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE guild_settings SET {', '.join(sets)}, updated_at = NOW() WHERE guild_id = $1",
                *values
            )
    
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
            query += f" AND guild_id = ${idx}"
            values.append(guild_id)
            idx += 1
        
        if actor_discord_id:
            query += f" AND actor_discord_id = ${idx}"
            values.append(actor_discord_id)
            idx += 1
        
        if action:
            query += f" AND action = ${idx}"
            values.append(action)
            idx += 1
        
        if target_type:
            query += f" AND target_type = ${idx}"
            values.append(target_type)
            idx += 1

        if search:
            query += f" AND (action ILIKE ${idx} OR target_type ILIKE ${idx} OR CAST(target_id AS TEXT) ILIKE ${idx} OR CAST(actor_discord_id AS TEXT) ILIKE ${idx})"
            values.append(f"%{search}%")
            idx += 1
        
        offset = (page - 1) * limit
        query += f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
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
            query += f" AND guild_id = ${idx}"
            values.append(guild_id)
            idx += 1
        
        if actor_discord_id:
            query += f" AND actor_discord_id = ${idx}"
            values.append(actor_discord_id)
            idx += 1
        
        if action:
            query += f" AND action = ${idx}"
            values.append(action)
            idx += 1

        if search:
            query += f" AND (action ILIKE ${idx} OR target_type ILIKE ${idx} OR CAST(target_id AS TEXT) ILIKE ${idx} OR CAST(actor_discord_id AS TEXT) ILIKE ${idx})"
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
        payment_item_id: Optional[int] = None,
        xanax_stack: Optional[str] = None
    ) -> int:
        """Create a new 99k jump session."""
        # Validate payment type
        if payment_type not in ('xanax', 'erotic_dvd'):
            raise ValueError(f"Invalid payment type: {payment_type}")
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO happy_jump_sessions (
                    guild_id, host_discord_id, host_torn_id, jump_type,
                    max_spots, xanax_count, xanax_stack, start_in_hours,
                    created_tct, estimated_jump_tct,
                    payment_type, payment_amount, payment_item_id, status
                ) VALUES ($1, $2, $3, '99k', $4, $5, $6, $7, $8, $9, $10, $11, $12, 'open')
                RETURNING id
            """, guild_id, host_discord_id, host_torn_id, max_spots, xanax_count,
                xanax_stack, start_in_hours, created_tct, estimated_jump_tct,
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
        """Update session fields."""
        if not kwargs:
            return
        
        sets = []
        values = [session_id]
        for i, (key, value) in enumerate(kwargs.items(), start=2):
            sets.append(f"{key} = ${i}")
            values.append(value)
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE happy_jump_sessions SET {', '.join(sets)}, updated_at = NOW() WHERE id = $1",
                *values
            )
    
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
            query += f" AND guild_id = ${idx}"
            values.append(guild_id)
            idx += 1
        
        if host_discord_id:
            query += f" AND host_discord_id = ${idx}"
            values.append(host_discord_id)
            idx += 1
        
        if status:
            query += f" AND status = ${idx}"
            values.append(status)
            idx += 1
        
        offset = (page - 1) * limit
        query += f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
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
            query += f" AND guild_id = ${idx}"
            values.append(guild_id)
            idx += 1
        
        if status:
            query += f" AND status = ${idx}"
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
        """Create a signup reservation."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO happy_jump_signups 
                (session_id, discord_id, torn_user_id, status, reserved_until)
                VALUES ($1, $2, $3, 'reserved', $4)
                RETURNING id
            """, session_id, discord_id, torn_user_id, reserved_until)
            return row['id']
    
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
        """Update signup fields."""
        if not kwargs:
            return
        
        sets = []
        values = [session_id, discord_id]
        for i, (key, value) in enumerate(kwargs.items(), start=3):
            sets.append(f"{key} = ${i}")
            values.append(value)
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE happy_jump_signups SET {', '.join(sets)} WHERE session_id = $1 AND discord_id = $2",
                *values
            )
    
    async def confirm_signup(self, session_id: int, discord_id: int):
        """Confirm a signup (payment verified)."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE happy_jump_signups
                SET status = 'confirmed', payment_verified = TRUE,
                    payment_verified_at = NOW(), reserved_until = NULL
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
    
    async def delete_signup(self, session_id: int, discord_id: int):
        """Delete a signup."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM happy_jump_signups
                WHERE session_id = $1 AND discord_id = $2
            """, session_id, discord_id)
    
    async def cleanup_expired_reservations(self) -> int:
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
        """Add user to session waitlist. Returns position."""
        async with self.pool.acquire() as conn:
            max_pos = await conn.fetchval("""
                SELECT COALESCE(MAX(position), 0) FROM happy_jump_waitlist
                WHERE session_id = $1
            """, session_id)
            new_pos = max_pos + 1
            
            await conn.execute("""
                INSERT INTO happy_jump_waitlist (session_id, discord_id, torn_user_id, position)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (session_id, discord_id) DO NOTHING
            """, session_id, discord_id, torn_user_id, new_pos)
            return new_pos
    
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
    
    # ========================================================================
    # INSURANCE POLICIES
    # ========================================================================
    
    async def create_policy(
        self,
        provider_id: int,
        name: str,
        description: Optional[str] = None,
        cost_type: str = 'xanax',
        cost_amount: int = 1,
        coverage_type: str = 'xanax_stack',
        payout_description: Optional[str] = None,
        duration_hours: int = 24,
        guild_id: Optional[int] = None
    ) -> int:
        """Create a new insurance policy."""
        # Validate cost type
        if cost_type not in ('xanax', 'erotic_dvd'):
            raise ValueError(f"Invalid cost type: {cost_type}")
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO insurance_policies (
                    provider_id, guild_id, name, description,
                    cost_type, cost_amount, coverage_type, payout_description,
                    duration_hours, active
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE)
                RETURNING policy_id
            """, provider_id, guild_id, name, description, cost_type, cost_amount,
                coverage_type, payout_description, duration_hours)
            return row['policy_id']
    
    async def get_policy(self, policy_id: int) -> Optional[Dict]:
        """Get policy with provider info."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT p.*, pr.discord_id as provider_discord_id,
                       pr.torn_user_id as provider_torn_id, pr.company_name
                FROM insurance_policies p
                JOIN insurance_providers pr ON pr.provider_id = p.provider_id
                WHERE p.policy_id = $1
            """, policy_id)
            return dict(row) if row else None
    
    async def get_active_policies(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Get all active policies."""
        query = """
            SELECT p.*, pr.discord_id as provider_discord_id,
                   pr.torn_user_id as provider_torn_id, pr.company_name
            FROM insurance_policies p
            JOIN insurance_providers pr ON pr.provider_id = p.provider_id
            WHERE p.active = TRUE AND pr.approval_status = 'approved' AND pr.active = TRUE
        """
        values = []
        
        if guild_id:
            query += " AND (p.guild_id IS NULL OR p.guild_id = $1)"
            values.append(guild_id)
        
        query += " ORDER BY p.created_at DESC"
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def get_provider_policies(self, provider_id: int) -> List[Dict]:
        """Get all policies for a provider."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM insurance_policies
                WHERE provider_id = $1
                ORDER BY created_at DESC
            """, provider_id)
            return [dict(row) for row in rows]
    
    async def update_policy(self, policy_id: int, **kwargs):
        """Update policy fields."""
        if not kwargs:
            return
        
        sets = []
        values = [policy_id]
        for i, (key, value) in enumerate(kwargs.items(), start=2):
            sets.append(f"{key} = ${i}")
            values.append(value)
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE insurance_policies SET {', '.join(sets)} WHERE policy_id = $1",
                *values
            )
    
    # ========================================================================
    # INSURANCE COVERAGE
    # ========================================================================
    
    async def create_coverage(
        self,
        policy_id: int,
        user_discord_id: int,
        user_torn_id: int,
        xanax_covered: int,
        premium_paid: int,
        payout_amount: int,
        duration_hours: int,
        reserved_until: Optional[datetime] = None
    ) -> int:
        """Create new insurance coverage (pending payment)."""
        expires_at = datetime.utcnow() + timedelta(hours=duration_hours)
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO insurance_coverage (
                    policy_id, user_discord_id, user_torn_id,
                    xanax_covered, premium_paid, payout_amount,
                    status, expires_at, reserved_until
                ) VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8)
                RETURNING coverage_id
            """, policy_id, user_discord_id, user_torn_id, xanax_covered,
                premium_paid, payout_amount, expires_at, reserved_until)
            return row['coverage_id']
    
    async def get_coverage(self, coverage_id: int) -> Optional[Dict]:
        """Get coverage with policy info."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT c.*, p.name as policy_name, p.coverage_type,
                       pr.company_name, pr.discord_id as provider_discord_id
                FROM insurance_coverage c
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                JOIN insurance_providers pr ON pr.provider_id = p.provider_id
                WHERE c.coverage_id = $1
            """, coverage_id)
            return dict(row) if row else None
    
    async def activate_coverage(self, coverage_id: int):
        """Activate coverage after payment verification."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_coverage
                SET status = 'active', activated_at = NOW(), reserved_until = NULL
                WHERE coverage_id = $1
            """, coverage_id)
    
    async def get_user_active_coverage(self, user_discord_id: int) -> List[Dict]:
        """Get user's active coverage."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, p.name as policy_name, p.coverage_type, pr.company_name
                FROM insurance_coverage c
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                JOIN insurance_providers pr ON pr.provider_id = p.provider_id
                WHERE c.user_discord_id = $1 AND c.status = 'active' AND c.expires_at > NOW()
                ORDER BY c.expires_at
            """, user_discord_id)
            return [dict(row) for row in rows]
    
    async def get_active_coverage(self) -> List[Dict]:
        """Get all active coverage for monitoring."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, p.coverage_type, p.provider_id
                FROM insurance_coverage c
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                WHERE c.status = 'active' AND c.expires_at > NOW()
            """)
            return [dict(row) for row in rows]
    
    async def expire_coverage(self) -> int:
        """Expire coverage past expiration date."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE insurance_coverage
                SET status = 'expired'
                WHERE status = 'active' AND expires_at < NOW()
            """)
            expired = int(result.split()[-1])
            if expired > 0:
                log.info(f"Expired {expired} insurance coverages")
            return expired
    
    async def update_coverage_last_check(self, coverage_id: int, log_timestamp: int):
        """Update last checked log timestamp for coverage."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_coverage
                SET last_log_timestamp = $2
                WHERE coverage_id = $1
            """, coverage_id, log_timestamp)
    
    async def cleanup_expired_coverage_reservations(self) -> int:
        """Clean up unpaid coverage reservations without crashing workers."""
        async with self.pool.acquire() as conn:
            try:
                result = await conn.execute("""
                    DELETE FROM insurance_coverage
                    WHERE status = 'pending' AND reserved_until < NOW()
                """)
            except asyncpg.UndefinedColumnError:
                log.warning(
                    "Skipping coverage reservation cleanup because reserved_until does not exist yet"
                )
                return 0

            deleted = int(result.split()[-1])
            if deleted > 0:
                log.info(f"Cleaned up {deleted} expired coverage reservations")
            return deleted
    
    # ========================================================================
    # INSURANCE CLAIMS
    # ========================================================================
    
    async def create_claim(
        self,
        coverage_id: int,
        policy_id: int,
        user_discord_id: int,
        provider_id: int,
        claim_type: str,
        xanax_lost: int,
        payout_amount: int,
        torn_log_id: Optional[int] = None,
        torn_log_timestamp: Optional[int] = None,
        torn_log_evidence: Optional[str] = None
    ) -> int:
        """Create an insurance claim."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO insurance_claims (
                    coverage_id, policy_id, user_discord_id, provider_id,
                    claim_type, xanax_lost, payout_amount,
                    torn_log_id, torn_log_timestamp, torn_log_evidence, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'pending')
                RETURNING claim_id
            """, coverage_id, policy_id, user_discord_id, provider_id,
                claim_type, xanax_lost, payout_amount,
                torn_log_id, torn_log_timestamp, torn_log_evidence)
            return row['claim_id']
    
    async def get_claim(self, claim_id: int) -> Optional[Dict]:
        """Get claim with related info."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT c.*, cov.user_torn_id, p.name as policy_name, pr.company_name,
                       pr.discord_id as provider_discord_id
                FROM insurance_claims c
                JOIN insurance_coverage cov ON cov.coverage_id = c.coverage_id
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                JOIN insurance_providers pr ON pr.provider_id = c.provider_id
                WHERE c.claim_id = $1
            """, claim_id)
            return dict(row) if row else None
    
    async def get_provider_claims(
        self,
        provider_id: int,
        status: Optional[str] = None
    ) -> List[Dict]:
        """Get claims for a provider."""
        query = """
            SELECT c.*, cov.user_discord_id, cov.user_torn_id, p.name as policy_name
            FROM insurance_claims c
            JOIN insurance_coverage cov ON cov.coverage_id = c.coverage_id
            JOIN insurance_policies p ON p.policy_id = c.policy_id
            WHERE c.provider_id = $1
        """
        values = [provider_id]
        
        if status:
            query += " AND c.status = $2"
            values.append(status)
        
        query += " ORDER BY c.created_at DESC"
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def get_pending_claims(self, guild_id: Optional[int] = None) -> List[Dict]:
        """Get all pending claims."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT c.*, cov.user_discord_id, cov.user_torn_id, 
                       p.name as policy_name, pr.company_name
                FROM insurance_claims c
                JOIN insurance_coverage cov ON cov.coverage_id = c.coverage_id
                JOIN insurance_policies p ON p.policy_id = c.policy_id
                JOIN insurance_providers pr ON pr.provider_id = c.provider_id
                WHERE c.status = 'pending'
                ORDER BY c.created_at
            """)
            return [dict(row) for row in rows]
    
    async def approve_claim(self, claim_id: int, resolved_by: int):
        """Approve a claim."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_claims
                SET status = 'approved', resolved_by = $2, resolved_at = NOW()
                WHERE claim_id = $1
            """, claim_id, resolved_by)
    
    async def reject_claim(self, claim_id: int, resolved_by: int, notes: Optional[str] = None):
        """Reject a claim."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_claims
                SET status = 'rejected', resolved_by = $2, resolved_at = NOW(), notes = $3
                WHERE claim_id = $1
            """, claim_id, resolved_by, notes)
    
    async def mark_claim_paid(self, claim_id: int):
        """Mark claim as paid."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE insurance_claims SET status = 'paid' WHERE claim_id = $1
            """, claim_id)
            
            # Also update coverage status
            await conn.execute("""
                UPDATE insurance_coverage
                SET status = 'claimed'
                WHERE coverage_id = (SELECT coverage_id FROM insurance_claims WHERE claim_id = $1)
            """, claim_id)
    
    async def check_existing_claim(self, coverage_id: int, torn_log_id: int) -> bool:
        """Check if a claim already exists for this log entry."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM insurance_claims
                WHERE coverage_id = $1 AND torn_log_id = $2
            """, coverage_id, torn_log_id)
            return count > 0
    
    # ========================================================================
    # RAFFLES
    # ========================================================================
    
    async def create_raffle(
        self,
        guild_id: int,
        creator_discord_id: int,
        prize: str,
        ticket_payment_type: str,
        ticket_price: int,
        tickets_available: int,
        max_tickets_per_user: int,
        end_time: datetime,
        ticket_payment_item_id: Optional[int] = None,
        channel_id: Optional[int] = None
    ) -> int:
        """Create a new raffle."""
        # Validate payment type
        if ticket_payment_type not in ('xanax', 'erotic_dvd'):
            raise ValueError(f"Invalid ticket payment type: {ticket_payment_type}")

        max_tickets_value = max_tickets_per_user if max_tickets_per_user and max_tickets_per_user > 0 else None

        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO raffles (
                    guild_id, creator_discord_id, prize,
                    ticket_payment_type, ticket_price, ticket_payment_item_id,
                    tickets_available, max_tickets_per_user,
                    status, end_time, announcement_channel_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'active', $9, $10)
                RETURNING raffle_id
            """, guild_id, creator_discord_id, prize, ticket_payment_type,
                ticket_price, ticket_payment_item_id, tickets_available,
                max_tickets_value, end_time, channel_id)
            return row['raffle_id']
    
    async def get_raffle(self, raffle_id: int) -> Optional[Dict]:
        """Get raffle by ID."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM raffles WHERE raffle_id = $1", raffle_id
            )
            return dict(row) if row else None
    
    async def update_raffle(self, raffle_id: int, **kwargs):
        """Update raffle fields."""
        if not kwargs:
            return
        
        sets = []
        values = [raffle_id]
        for i, (key, value) in enumerate(kwargs.items(), start=2):
            sets.append(f"{key} = ${i}")
            values.append(value)
        
        async with self.pool.acquire() as conn:
            await conn.execute(
                f"UPDATE raffles SET {', '.join(sets)} WHERE raffle_id = $1",
                *values
            )
    
    async def get_active_raffles(self, guild_id: int) -> List[Dict]:
        """Get active raffles for a guild."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM raffles
                WHERE guild_id = $1 AND status IN ('active', 'open')
                ORDER BY end_time
            """, guild_id)
            return [dict(row) for row in rows]
    
    async def has_active_raffle(self, creator_discord_id: int) -> bool:
        """Check if user has an active raffle (for 1-per-admin rule)."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM raffles
                WHERE creator_discord_id = $1 AND status IN ('active', 'open')
            """, creator_discord_id)
            return count > 0
    
    async def get_admin_active_raffle(
        self,
        guild_id: int,
        creator_discord_id: int
    ) -> Optional[Dict]:
        """Get admin's active raffle in a guild."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM raffles
                WHERE guild_id = $1 AND creator_discord_id = $2 AND status IN ('active', 'open')
            """, guild_id, creator_discord_id)
            return dict(row) if row else None
    
    async def get_raffles_to_draw(self) -> List[Dict]:
        """Get raffles past their end time that need drawing."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM raffles
                WHERE status IN ('active', 'open') AND end_time <= NOW()
            """)
            return [dict(row) for row in rows]
    
    async def get_raffle_history(
        self,
        guild_id: int,
        status: Optional[str] = None,
        limit: int = 50,
        page: int = 1
    ) -> List[Dict]:
        """Get raffle history with filters and pagination."""
        query = "SELECT * FROM raffles WHERE guild_id = $1"
        values = [guild_id]
        idx = 2
        
        if status:
            query += f" AND status = ${idx}"
            values.append(status)
            idx += 1
        
        offset = (page - 1) * limit
        query += f" ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx + 1}"
        values.extend([limit, offset])
        
        async with self.pool.acquire() as conn:
            try:
                rows = await conn.fetch(query, *values)
            except asyncpg.PostgresError:
                log.warning("Audit log query failed; returning empty entries", exc_info=True)
                return []
            return [dict(row) for row in rows]
    
    async def get_raffle_count(
        self,
        guild_id: int,
        status: Optional[str] = None
    ) -> int:
        """Get total count of raffles."""
        query = "SELECT COUNT(*) FROM raffles WHERE guild_id = $1"
        values = [guild_id]
        
        if status:
            query += " AND status = $2"
            values.append(status)
        
        async with self.pool.acquire() as conn:
            try:
                return int(await conn.fetchval(query, *values) or 0)
            except asyncpg.PostgresError:
                log.warning("Audit log count query failed; returning 0", exc_info=True)
                return 0
    
    # ========================================================================
    # RAFFLE ENTRIES
    # ========================================================================
    
    async def create_raffle_entry(
        self,
        raffle_id: int,
        discord_id: int,
        torn_user_id: int,
        num_tickets: int,
        reserved_until: datetime
    ) -> int:
        """Create or update raffle entry."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO raffle_entries 
                (raffle_id, discord_id, torn_user_id, num_tickets, reserved_until, payment_verified)
                VALUES ($1, $2, $3, $4, $5, FALSE)
                ON CONFLICT (raffle_id, discord_id) DO UPDATE SET
                    num_tickets = raffle_entries.num_tickets + EXCLUDED.num_tickets,
                    reserved_until = EXCLUDED.reserved_until
                RETURNING entry_id
            """, raffle_id, discord_id, torn_user_id, num_tickets, reserved_until)
            return row['entry_id']
    
    async def get_raffle_entry(self, raffle_id: int, discord_id: int) -> Optional[Dict]:
        """Get specific raffle entry."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM raffle_entries
                WHERE raffle_id = $1 AND discord_id = $2
            """, raffle_id, discord_id)
            return dict(row) if row else None
    
    async def verify_raffle_payment(self, raffle_id: int, discord_id: int):
        """Mark raffle entry as paid."""
        async with self.pool.acquire() as conn:
            # Get current entry tickets
            entry = await conn.fetchrow("""
                SELECT num_tickets FROM raffle_entries
                WHERE raffle_id = $1 AND discord_id = $2
            """, raffle_id, discord_id)
            
            if entry:
                await conn.execute("""
                    UPDATE raffle_entries
                    SET payment_verified = TRUE, payment_verified_at = NOW(), reserved_until = NULL
                    WHERE raffle_id = $1 AND discord_id = $2
                """, raffle_id, discord_id)
                
                # Update tickets sold count
                await conn.execute("""
                    UPDATE raffles
                    SET tickets_sold = tickets_sold + $2
                    WHERE raffle_id = $1
                """, raffle_id, entry['num_tickets'])
    
    async def get_raffle_entries(self, raffle_id: int) -> List[Dict]:
        """Get all entries for a raffle."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM raffle_entries
                WHERE raffle_id = $1
                ORDER BY created_at
            """, raffle_id)
            return [dict(row) for row in rows]
    
    async def get_raffle_paid_entries(self, raffle_id: int) -> List[Dict]:
        """Get paid entries for a raffle."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM raffle_entries
                WHERE raffle_id = $1 AND payment_verified = TRUE
                ORDER BY created_at
            """, raffle_id)
            return [dict(row) for row in rows]
    
    async def get_raffle_ticket_count(self, raffle_id: int) -> int:
        """Get total paid tickets for a raffle."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COALESCE(SUM(num_tickets), 0) FROM raffle_entries
                WHERE raffle_id = $1 AND payment_verified = TRUE
            """, raffle_id)
            return int(count)
    
    async def get_user_raffle_tickets(self, raffle_id: int, discord_id: int) -> int:
        """Get user's ticket count for a raffle."""
        async with self.pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COALESCE(num_tickets, 0) FROM raffle_entries
                WHERE raffle_id = $1 AND discord_id = $2 AND payment_verified = TRUE
            """, raffle_id, discord_id)
            return int(count or 0)
    
    async def delete_raffle_entry(self, raffle_id: int, discord_id: int):
        """Delete a raffle entry."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM raffle_entries
                WHERE raffle_id = $1 AND discord_id = $2
            """, raffle_id, discord_id)
    
    async def cleanup_expired_raffle_entries(self) -> int:
        """Clean up unpaid raffle entry reservations."""
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM raffle_entries
                WHERE payment_verified = FALSE AND reserved_until < NOW()
            """)
            deleted = int(result.split()[-1])
            if deleted > 0:
                log.info(f"Cleaned up {deleted} expired raffle entry reservations")
            return deleted
    
    async def draw_raffle_winner(self, raffle_id: int) -> Optional[Dict]:
        """Draw a winner for a raffle using weighted random selection."""
        entries = await self.get_raffle_paid_entries(raffle_id)
        if not entries:
            # No entries, mark as completed with no winner
            await self.update_raffle(raffle_id, status='completed', drawn_at=datetime.utcnow())
            return None
        
        # Build weighted ticket pool
        ticket_pool = []
        ticket_number = 1
        for entry in entries:
            for _ in range(entry['num_tickets']):
                ticket_pool.append({
                    'discord_id': entry['discord_id'],
                    'torn_user_id': entry['torn_user_id'],
                    'ticket_number': ticket_number
                })
                ticket_number += 1
        
        # Draw random winner
        winner_ticket = random.choice(ticket_pool)
        
        # Update raffle with winner
        await self.update_raffle(
            raffle_id,
            status='completed',
            winner_discord_id=winner_ticket['discord_id'],
            winner_torn_id=winner_ticket['torn_user_id'],
            winner_ticket_number=winner_ticket['ticket_number'],
            drawn_at=datetime.utcnow()
        )
        
        return {
            'discord_id': winner_ticket['discord_id'],
            'torn_user_id': winner_ticket['torn_user_id'],
            'ticket_number': winner_ticket['ticket_number'],
            'total_tickets': len(ticket_pool)
        }
    
    async def cancel_raffle(self, raffle_id: int):
        """Cancel a raffle."""
        await self.update_raffle(raffle_id, status='cancelled')
    
    # ========================================================================
    # DASHBOARD SESSIONS
    # ========================================================================
    
    async def create_dashboard_session(
        self,
        session_id: str,
        discord_id: int,
        session_data: Dict,
        expires_at: datetime
    ):
        """Create or update dashboard session."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO dashboard_sessions (session_id, discord_id, session_data, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (session_id) DO UPDATE SET
                    session_data = EXCLUDED.session_data,
                    expires_at = EXCLUDED.expires_at,
                    last_accessed_at = NOW()
            """, session_id, discord_id, json.dumps(session_data), expires_at)
    
    async def get_dashboard_session(self, session_id: str) -> Optional[Dict]:
        """Get dashboard session."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM dashboard_sessions
                WHERE session_id = $1 AND expires_at > NOW()
            """, session_id)
            if row:
                result = dict(row)
                result['session_data'] = json.loads(result['session_data'])
                return result
            return None
    
    async def delete_dashboard_session(self, session_id: str):
        """Delete dashboard session."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM dashboard_sessions WHERE session_id = $1", session_id
            )
    
    async def cleanup_expired_dashboard_sessions(self) -> int:
        """Clean up expired dashboard sessions."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM dashboard_sessions WHERE expires_at < NOW()"
            )
            return int(result.split()[-1])
    
    # ========================================================================
    # STATISTICS
    # ========================================================================
    
    async def get_guild_statistics(self, guild_id: int) -> Dict:
        """Get statistics for a guild."""
        async with self.pool.acquire() as conn:
            sessions = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                    COUNT(CASE WHEN status = 'cancelled' THEN 1 END) as cancelled,
                    COUNT(CASE WHEN status IN ('open', 'locked') THEN 1 END) as active
                FROM happy_jump_sessions WHERE guild_id = $1
            """, guild_id)
            
            raffles = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                    COUNT(CASE WHEN status = 'active' THEN 1 END) as active
                FROM raffles WHERE guild_id = $1
            """, guild_id)
            
            return {
                'sessions': dict(sessions),
                'raffles': dict(raffles)
            }
    
    async def get_dashboard_stats(self, guild_id: int) -> Dict:
        """Get dashboard KPI stats with schema-tolerant fallbacks."""

        async def _safe_count(conn: asyncpg.Connection, query: str, *args: Any) -> int:
            try:
                value = await conn.fetchval(query, *args)
                return int(value or 0)
            except asyncpg.PostgresError:
                log.warning("Dashboard stats query failed; defaulting to 0", exc_info=True)
                return 0

        async with self.pool.acquire() as conn:
            active_sessions = await _safe_count(
                conn,
                """
                SELECT COUNT(*) FROM happy_jump_sessions
                WHERE guild_id = $1 AND status IN ('open', 'locked')
                """,
                guild_id,
            )

            active_raffles = await _safe_count(
                conn,
                """
                SELECT COUNT(*) FROM raffles
                WHERE guild_id = $1 AND status IN ('active', 'open')
                """,
                guild_id,
            )

            active_policies = await _safe_count(
                conn,
                """
                SELECT COUNT(*) FROM insurance_policies p
                JOIN insurance_providers pr ON pr.provider_id = p.provider_id
                WHERE p.active = TRUE AND pr.approval_status = 'approved' AND p.guild_id = $1
                """,
                guild_id,
            )

            pending_claims = await _safe_count(
                conn,
                """
                SELECT COUNT(*) FROM insurance_claims c
                JOIN insurance_policies p ON p.id = c.policy_id
                WHERE c.status = 'pending' AND p.guild_id = $1
                """,
                guild_id,
            )

            return {
                'active_sessions': active_sessions,
                'active_raffles': active_raffles,
                'active_policies': active_policies,
                'pending_claims': pending_claims,
            }

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
        await _db_manager.init_pool(run_migrations=config.RUN_MIGRATIONS_ON_STARTUP)
        _db_loop = current_loop
        return _db_manager


def get_database() -> DatabaseManager:
    """Get the database manager singleton."""
    if _db_manager is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db_manager
