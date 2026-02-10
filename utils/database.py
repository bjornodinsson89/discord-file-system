"""
Happy Jumper Discord Bot - Database compatibility shim
Delegates to repository layer while maintaining backward compatibility
"""

import logging
import asyncpg
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import config

log = logging.getLogger("happy_jumper.database")

# Database pool singleton
_pool: Optional[asyncpg.Pool] = None

class DatabaseManager:
    """Legacy database manager - delegates to repositories."""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        log.warning("utils.database is a compatibility shim; migrate callers to repositories/* modules")
    
    # ============================================================================
    # RAFFLE METHODS (NEW - for sell-out trigger feature)
    # ============================================================================
    
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
        """Create a new raffle. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.create_raffle(
            guild_id=guild_id,
            creator_discord_id=creator_discord_id,
            prize=prize,
            ticket_payment_type=ticket_payment_type,
            ticket_price=ticket_price,
            tickets_available=tickets_available,
            max_tickets_per_user=max_tickets_per_user,
            end_time=end_time,
            end_trigger=end_trigger,
            hours_after_sold_out=hours_after_sold_out,
        )
    
    async def expire_coverage(self) -> int:
        """Expire old insurance coverage. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.expire_coverage()
    
    async def get_active_coverage(self) -> List[Dict[str, Any]]:
        """Get active insurance coverage. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.get_active_coverage()
    
    async def check_existing_claim(self, coverage_id: int, log_id: int) -> bool:
        """Check if claim exists. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.check_existing_claim(coverage_id, log_id)
    
    async def update_coverage_last_check(self, coverage_id: int, timestamp: int) -> None:
        """Update coverage last check timestamp. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        await repo.update_coverage_last_check(coverage_id, timestamp)
    
    async def get_policy(self, policy_id: int) -> Optional[Dict[str, Any]]:
        """Get policy by ID. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.get_policy(policy_id)
    
    async def get_provider_by_id(self, provider_id: int) -> Optional[Dict[str, Any]]:
        """Get provider by ID. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.get_provider_by_id(provider_id)
    
    async def create_claim(self, **kwargs) -> int:
        """Create insurance claim. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.create_claim(**kwargs)
    
    async def get_claim(self, claim_id: int) -> Optional[Dict[str, Any]]:
        """Get claim by ID. Delegates to InsuranceRepository."""
        from repositories.insurance import InsuranceRepository
        repo = InsuranceRepository(self.pool)
        return await repo.get_claim(claim_id)
    
    async def get_raffles_to_draw(self) -> List[Dict[str, Any]]:
        """Get raffles ready to draw. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.get_raffles_to_draw()
    
    async def draw_raffle_winner(self, raffle_id: int) -> Optional[Dict[str, Any]]:
        """Draw raffle winner. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.draw_raffle_winner(raffle_id)
    
    async def update_raffle(self, raffle_id: int, **fields) -> bool:
        """Update raffle fields. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.update_raffle(raffle_id, **fields)
    
    async def get_raffle(self, raffle_id: int) -> Optional[Dict[str, Any]]:
        """Get raffle by ID. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.get_raffle(raffle_id)
    
    async def get_raffle_entries(self, raffle_id: int) -> List[Dict[str, Any]]:
        """Get raffle entries. Delegates to RafflesRepository."""
        from repositories.raffles import RafflesRepository
        repo = RafflesRepository(self.pool)
        return await repo.get_raffle_entries(raffle_id)
    
    async def log_audit(self, actor_discord_id, action, target_type, target_id, 
                       payload=None, guild_id=None, source=None, **kwargs) -> int:
        """Log audit event. Delegates to AuditRepository."""
        from repositories.audit import AuditRepository
        repo = AuditRepository(self.pool)
        return await repo.log_audit(
            actor_discord_id=actor_discord_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            guild_id=guild_id,
            source=source
        )
    
    # ============================================================================
    # EXISTING LEGACY METHODS (keep for backward compatibility)
    # ============================================================================
    
    async def cleanup_expired_signups(self) -> int:
        """Clean up expired signups."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM happy_jump_signups 
                WHERE status = 'reserved' 
                AND reserved_until < NOW()
                """
            )
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0
    
    async def get_sessions_with_expired_signups(self):
        """Get sessions with expired signups."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT s.* FROM happy_jump_sessions s
                JOIN happy_jump_signups su ON s.id = su.session_id
                WHERE su.status = 'reserved' 
                AND su.reserved_until < NOW()
                AND s.status = 'open'
                """
            )
            return [dict(row) for row in rows]
    
    async def get_session_signups(self, session_id: int):
        """Get all signups for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM happy_jump_signups WHERE session_id = $1",
                session_id
            )
            return [dict(row) for row in rows]
    
    async def get_confirmed_signups(self, session_id: int):
        """Get confirmed signups for a session."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM happy_jump_signups WHERE session_id = $1 AND status IN ('confirmed', 'reserved')",
                session_id
            )
            return [dict(row) for row in rows]
    
    async def promote_from_waitlist(self, session_id: int):
        """Promote first person from waitlist."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Get first waitlist entry
                row = await conn.fetchrow(
                    """
                    SELECT * FROM happy_jump_waitlist 
                    WHERE session_id = $1 
                    ORDER BY position ASC, added_at ASC
                    LIMIT 1
                    """,
                    session_id
                )
                if not row:
                    return None
                
                # Remove from waitlist
                await conn.execute(
                    "DELETE FROM happy_jump_waitlist WHERE id = $1",
                    row['id']
                )
                
                # Add to signups
                await conn.execute(
                    """
                    INSERT INTO happy_jump_signups (session_id, discord_id, torn_user_id, status, reserved_until)
                    VALUES ($1, $2, $3, 'reserved', NOW() + INTERVAL '30 minutes')
                    ON CONFLICT (session_id, discord_id) DO UPDATE
                    SET status = 'reserved', reserved_until = EXCLUDED.reserved_until
                    """,
                    session_id,
                    row['discord_id'],
                    row['torn_id']
                )
                
                return dict(row)
    
    async def get_active_sessions(self, guild_id: int):
        """Get active sessions for a guild."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM happy_jump_sessions WHERE guild_id = $1 AND status = 'open'",
                guild_id
            )
            return [dict(row) for row in rows]
    
    async def get_signup(self, session_id: int, discord_id: int):
        """Get specific signup."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM happy_jump_signups WHERE session_id = $1 AND discord_id = $2",
                session_id,
                discord_id
            )
            return dict(row) if row else None
    
    async def get_waitlist_position(self, session_id: int, discord_id: int):
        """Get waitlist position for a user."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchval(
                """
                SELECT position FROM happy_jump_waitlist 
                WHERE session_id = $1 AND discord_id = $2
                """,
                session_id,
                discord_id
            )
            return row
    
    async def get_user_api_key(self, discord_id: int):
        """Get user's API key."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM user_api_keys WHERE discord_id = $1",
                discord_id
            )
            return dict(row) if row else None
    
    async def update_readiness(self, session_id: int, discord_id: int, **kwargs):
        """Update readiness status."""
        async with self.pool.acquire() as conn:
            sets = []
            values = []
            for i, (key, value) in enumerate(kwargs.items(), 1):
                sets.append(f"{key} = ${i}")
                values.append(value)
            values.extend([session_id, discord_id])
            
            query = f"""
                UPDATE happy_jump_readiness 
                SET {', '.join(sets)}, checked_at = NOW()
                WHERE session_id = ${len(values)-1} AND discord_id = ${len(values)}
            """
            await conn.execute(query, *values)
    
    async def get_guild_settings(self, guild_id: int):
        """Get guild settings."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM guild_settings WHERE guild_id = $1",
                guild_id
            )
            return dict(row) if row else {}
    
    async def update_guild_settings(self, guild_id: int, **fields):
        """Update guild settings."""
        async with self.pool.acquire() as conn:
            sets = []
            values = []
            for i, (key, value) in enumerate(fields.items(), 1):
                sets.append(f"{key} = ${i}")
                values.append(value)
            values.append(guild_id)
            
            query = f"UPDATE guild_settings SET {', '.join(sets)} WHERE guild_id = ${len(values)}"
            await conn.execute(query, *values)
    
    async def get_guild_statistics(self, guild_id: int):
        """Get guild statistics."""
        async with self.pool.acquire() as conn:
            stats = {}
            # Session count
            stats['total_sessions'] = await conn.fetchval(
                "SELECT COUNT(*) FROM happy_jump_sessions WHERE guild_id = $1",
                guild_id
            ) or 0
            
            # Raffle count
            stats['total_raffles'] = await conn.fetchval(
                "SELECT COUNT(*) FROM raffles WHERE guild_id = $1",
                guild_id
            ) or 0
            
            # Insurance policies
            stats['total_policies'] = await conn.fetchval(
                "SELECT COUNT(*) FROM insurance_policies WHERE guild_id = $1",
                guild_id
            ) or 0
            
            return stats
    
    async def list_pending_insurer_applications(self):
        """List pending insurer applications."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT provider_id, guild_id, discord_id, torn_user_id, 
                       company_name as torn_name, application_data,
                       approval_status, created_at
                FROM insurance_providers
                WHERE approval_status = 'pending'
                """
            )
            return [dict(row) for row in rows]
    
    async def list_pending_host_applications(self):
        """List pending host applications."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, guild_id, discord_id, torn_user_id, torn_name,
                       application_data, approval_status, created_at
                FROM host_applications
                WHERE approval_status = 'pending'
                """
            )
            return [dict(row) for row in rows]
    
    async def upsert_host_application(self, **kwargs):
        """Create or update host application."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO host_applications 
                    (guild_id, discord_id, torn_user_id, torn_name, display_name,
                     forum_url, application_data, approval_status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', NOW(), NOW())
                ON CONFLICT (guild_id, discord_id) DO UPDATE
                SET torn_user_id = EXCLUDED.torn_user_id,
                    torn_name = EXCLUDED.torn_name,
                    display_name = EXCLUDED.display_name,
                    forum_url = EXCLUDED.forum_url,
                    application_data = EXCLUDED.application_data,
                    approval_status = 'pending',
                    updated_at = NOW()
                RETURNING *
                """,
                kwargs['guild_id'],
                kwargs['discord_id'],
                kwargs['torn_user_id'],
                kwargs['torn_name'],
                kwargs.get('display_name'),
                kwargs['forum_url'],
                kwargs['application_data']
            )
            return dict(row) if row else None
    
    async def try_advisory_lock(self, lock_key: int) -> bool:
        """Try to acquire advisory lock."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)",
                lock_key
            )
            return result
    
    async def release_advisory_lock(self, lock_key: int):
        """Release advisory lock."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "SELECT pg_advisory_unlock($1)",
                lock_key
            )
    
    async def get_audit_logs(self, guild_id: int = None, limit: int = 20):
        """Get audit logs."""
        async with self.pool.acquire() as conn:
            if guild_id:
                rows = await conn.fetch(
                    """
                    SELECT * FROM audit_log 
                    WHERE guild_id = $1 
                    ORDER BY created_at DESC 
                    LIMIT $2
                    """,
                    guild_id,
                    limit
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT $1",
                    limit
                )
            return [dict(row) for row in rows]
    
    async def table_exists(self, table_name: str) -> bool:
        """Check if table exists."""
        async with self.pool.acquire() as conn:
            result = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = $1
                )
                """,
                table_name
            )
            return result


# ============================================================================
# PUBLIC API
# ============================================================================

async def init_database():
    """Initialize database pool."""
    global _pool
    if _pool is None:
        log.info("Initializing DB pool (ssl_mode=require)")
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=2,
            max_size=10,
            command_timeout=60,
            server_settings={
                'jit': 'off',
                'application_name': 'happy_jumper'
            }
        )
        log.info("Database pool initialized with PgBouncer-safe settings")


def get_database() -> DatabaseManager:
    """Get database manager instance."""
    if _pool is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return DatabaseManager(_pool)


def get_pool() -> asyncpg.Pool:
    """Get raw pool for repositories."""
    if _pool is None:
        raise RuntimeError("Database not initialized")
    return _pool
