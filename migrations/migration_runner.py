"""
Migration Runner for Happy Jumper Bot
Handles database migrations with PgBouncer/Supabase pooler compatibility.

Usage:
    python migration_runner.py migrate   # Run pending migrations
    python migration_runner.py fresh     # Fresh install (000_full_schema.sql)
    python migration_runner.py verify    # Verify schema
    python migration_runner.py reset     # DANGER: Drop all tables and recreate
"""

import asyncio
import asyncpg
import logging
import sys
from pathlib import Path
from typing import List, Tuple, Set

# Add parent directory to path for config import
sys.path.insert(0, str(Path(__file__).parent.parent))

import config

log = logging.getLogger("happy_jumper.migrations")


async def get_connection() -> asyncpg.Connection:
    """Get a database connection with PgBouncer-safe settings."""
    ssl_mode = config.get_db_ssl_config()
    
    return await asyncpg.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        ssl=ssl_mode,
        # CRITICAL: These settings are required for PgBouncer/Supabase pooler
        statement_cache_size=0,
    )


async def get_pool() -> asyncpg.Pool:
    """Get a connection pool with PgBouncer-safe settings."""
    ssl_mode = config.get_db_ssl_config()
    
    return await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        ssl=ssl_mode,
        min_size=1,
        max_size=5,
        # CRITICAL: These settings are required for PgBouncer/Supabase pooler
        statement_cache_size=0,
        command_timeout=60,
    )


class MigrationRunner:
    """Manages database schema migrations."""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
        self.migrations_dir = Path(__file__).parent
    
    async def init_migrations_table(self):
        """Create migrations tracking table and backfill optional columns."""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(50) PRIMARY KEY,
                    applied_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await conn.execute(
                "ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS description TEXT"
            )

    async def _migrations_has_description(self, conn: asyncpg.Connection) -> bool:
        """Return True when schema_migrations.description exists."""
        return bool(await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'schema_migrations'
                  AND column_name = 'description'
            )
        """))
    
    async def get_applied_migrations(self) -> Set[str]:
        """Get set of applied migration versions."""
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("SELECT version FROM schema_migrations")
                return {row['version'] for row in rows}
        except asyncpg.UndefinedTableError:
            return set()
    
    def get_available_migrations(self) -> List[Tuple[str, Path]]:
        """
        Get list of available migrations from filesystem.
        Returns: List of (version, filepath) tuples, sorted by version.
        """
        migrations = []
        for file in sorted(self.migrations_dir.glob("*.sql")):
            # Skip non-numbered files and the full schema
            if not file.name[0].isdigit():
                continue
            if file.name.startswith("000"):
                continue  # Skip full schema for incremental migrations
            
            version = file.stem  # e.g., "001_initial_schema"
            migrations.append((version, file))
        
        return migrations
    
    async def run_migration_file(self, filepath: Path, version: str) -> bool:
        """Run a single migration file."""
        log.info(f"Running migration: {filepath.name}")
        
        try:
            sql = filepath.read_text()
            async with self.pool.acquire() as conn:
                await conn.execute(sql)
                if await self._migrations_has_description(conn):
                    await conn.execute("""
                        INSERT INTO schema_migrations (version, applied_at, description)
                        VALUES ($1, NOW(), $2)
                        ON CONFLICT (version) DO NOTHING
                    """, version, f"Migration {filepath.name}")
                else:
                    await conn.execute("""
                        INSERT INTO schema_migrations (version, applied_at)
                        VALUES ($1, NOW())
                        ON CONFLICT (version) DO NOTHING
                    """, version)
            
            log.info(f"  ✓ {filepath.name} completed")
            return True
        except Exception as e:
            log.error(f"  ✗ {filepath.name} failed: {e}")
            return False
    
    async def run_pending_migrations(self) -> int:
        """Run all pending migrations. Returns count of applied migrations."""
        await self.init_migrations_table()
        
        applied = await self.get_applied_migrations()
        available = self.get_available_migrations()
        
        pending = [(v, p) for v, p in available if v not in applied]
        
        if not pending:
            log.info("No pending migrations")
            return 0
        
        log.info(f"Found {len(pending)} pending migrations")
        
        count = 0
        for version, filepath in pending:
            success = await self.run_migration_file(filepath, version)
            if success:
                count += 1
            else:
                log.error(f"Migration stopped at {filepath.name}")
                break
        
        return count
    
    async def run_fresh_install(self) -> bool:
        """Run 000_full_schema.sql for fresh install."""
        full_schema = self.migrations_dir / "000_full_schema.sql"
        
        if not full_schema.exists():
            log.error("000_full_schema.sql not found")
            return False
        
        log.info("Running fresh install from 000_full_schema.sql")
        
        try:
            sql = full_schema.read_text()
            async with self.pool.acquire() as conn:
                await conn.execute(sql)
            
            log.info("✓ Fresh install completed")
            return True
        except Exception as e:
            log.error(f"Fresh install failed: {e}")
            return False
    
    async def verify_schema(self) -> bool:
        """Verify the database schema is correct."""
        tables = [
            'user_api_keys', 'guild_settings', 'happy_jump_sessions',
            'happy_jump_signups', 'happy_jump_waitlist', 'happy_jump_readiness',
            'insurance_providers', 'insurance_policies', 'insurance_coverage',
            'insurance_claims', 'raffles', 'raffle_entries', 'audit_log',
            'blacklist', 'host_reputation', 'host_ratings', 'host_applications'
        ]
        
        all_good = True
        
        async with self.pool.acquire() as conn:
            for table in tables:
                try:
                    count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                    log.info(f"  ✓ {table}: {count} rows")
                except Exception as e:
                    log.error(f"  ✗ {table}: {e}")
                    all_good = False
        
        return all_good


async def run_migrations(pool: asyncpg.Pool) -> int:
    """Convenience function to run all pending migrations."""
    runner = MigrationRunner(pool)
    return await runner.run_pending_migrations()


async def run_fresh_install_standalone() -> bool:
    """Run fresh install as standalone script."""
    pool = await get_pool()
    try:
        runner = MigrationRunner(pool)
        return await runner.run_fresh_install()
    finally:
        await pool.close()


async def run_migrations_standalone() -> int:
    """Run migrations as standalone script."""
    pool = await get_pool()
    try:
        runner = MigrationRunner(pool)
        return await runner.run_pending_migrations()
    finally:
        await pool.close()


async def verify_schema_standalone() -> bool:
    """Verify schema as standalone script."""
    pool = await get_pool()
    try:
        runner = MigrationRunner(pool)
        return await runner.verify_schema()
    finally:
        await pool.close()


async def reset_database_standalone() -> bool:
    """
    DANGER: Completely reset the database.
    This drops all tables and recreates from scratch.
    """
    print("\n" + "="*60)
    print("WARNING: This will DELETE ALL DATA!")
    print("="*60 + "\n")
    
    confirm = input("Type 'YES DELETE EVERYTHING' to confirm: ")
    if confirm != "YES DELETE EVERYTHING":
        print("Aborted.")
        return False
    
    pool = await get_pool()
    
    try:
        tables = [
            'audit_log', 'raffle_entries', 'raffles',
            'insurance_claims', 'insurance_coverage', 'insurance_policies',
            'insurance_providers', 'blacklist', 'host_ratings', 'host_reputation',
            'happy_jump_readiness', 'happy_jump_waitlist', 'happy_jump_signups',
            'happy_jump_sessions', 'guild_settings', 'user_api_keys', 'schema_migrations'
        ]
        
        print("\nDropping tables...")
        async with pool.acquire() as conn:
            for table in tables:
                try:
                    await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
                    print(f"  ✓ Dropped {table}")
                except Exception as e:
                    print(f"  ✗ Failed to drop {table}: {e}")
            
            # Drop functions
            await conn.execute("DROP FUNCTION IF EXISTS update_updated_at_column() CASCADE")
        
        print("\nTables dropped. Running fresh install...")
        
        runner = MigrationRunner(pool)
        return await runner.run_fresh_install()
        
    finally:
        await pool.close()


def main():
    """CLI entry point."""
    import argparse
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s"
    )
    
    parser = argparse.ArgumentParser(description="Happy Jumper Database Migrations")
    parser.add_argument(
        "command",
        choices=["migrate", "fresh", "verify", "reset"],
        help="Command to run: migrate (incremental), fresh (full schema), verify, reset (DANGER)"
    )
    
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"Happy Jumper Migration Runner - {args.command.upper()}")
    print(f"{'='*60}\n")
    
    if args.command == "migrate":
        result = asyncio.run(run_migrations_standalone())
        print(f"\nApplied {result} migrations")
    elif args.command == "fresh":
        success = asyncio.run(run_fresh_install_standalone())
        print(f"\nFresh install: {'SUCCESS' if success else 'FAILED'}")
    elif args.command == "verify":
        success = asyncio.run(verify_schema_standalone())
        print(f"\nSchema verification: {'PASSED' if success else 'FAILED'}")
    elif args.command == "reset":
        success = asyncio.run(reset_database_standalone())
        print(f"\nDatabase reset: {'SUCCESS' if success else 'FAILED'}")


if __name__ == "__main__":
    main()
