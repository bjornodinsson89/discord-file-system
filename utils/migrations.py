from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger("happy_jumper.migrations")

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_LOCK_KEY = 864201357918426513


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied SQL migrations from /migrations in filename order."""
    migration_files = sorted(path for path in _MIGRATIONS_DIR.glob("*.sql") if path.is_file())
    if not migration_files:
        log.info("No migration files found in %s", _MIGRATIONS_DIR)
        return

    async with pool.acquire() as conn:
        lock_acquired = False
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
            lock_acquired = True

            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS public.schema_migrations (
                    version text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )

            applied_versions = {
                row["version"]
                for row in await conn.fetch("SELECT version FROM public.schema_migrations")
            }

            for migration_path in migration_files:
                version = migration_path.name
                if version in applied_versions:
                    continue

                script = migration_path.read_text(encoding="utf-8").strip()
                if not script:
                    log.warning("Skipping empty migration file: %s", version)
                    continue

                log.info("Applying migration %s", version)
                try:
                    async with conn.transaction():
                        await conn.execute(script)
                        await conn.execute(
                            "INSERT INTO public.schema_migrations (version) VALUES ($1)",
                            version,
                        )
                except Exception:
                    log.exception("Failed applying migration %s", version)
                    raise
        finally:
            if lock_acquired:
                try:
                    await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
                except Exception:
                    log.exception("Failed to release migration advisory lock")
