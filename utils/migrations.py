from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

import config

log = logging.getLogger("happy_jumper.migrations")

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_LOCK_KEY = 864201357918426513

_YYYYMMDD_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:_|$)")
_YYYY_MM_DD_RE = re.compile(r"^(\d{4})_(\d{2})_(\d{2})(?:_|$)")
_NNN_RE = re.compile(r"^(\d{3})(?:_|$)")


def _parse_migration_key(name: str) -> tuple[tuple[int, int, int], str, str]:
    """Return sort key that supports YYYYMMDD_* and YYYY_MM_DD_* prefixes."""
    match = _YYYYMMDD_RE.match(name)
    if match:
        yyyy, mm, dd = (int(match.group(i)) for i in range(1, 4))
        return (yyyy, mm, dd), name[match.end() :], name

    match = _YYYY_MM_DD_RE.match(name)
    if match:
        yyyy, mm, dd = (int(match.group(i)) for i in range(1, 4))
        return (yyyy, mm, dd), name[match.end() :], name

    match = _NNN_RE.match(name)
    if match:
        return (0, 0, int(match.group(1))), name[match.end() :], name

    return (9999, 12, 31), name, name


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply unapplied SQL migrations from /migrations in filename order."""
    migration_files = sorted(
        (path for path in _MIGRATIONS_DIR.glob("*.sql") if path.is_file()),
        key=lambda path: _parse_migration_key(path.name),
    )
    if not migration_files:
        log.info("No migration files found in %s", _MIGRATIONS_DIR)
        return

    async with pool.acquire(timeout=config.DB_ACQUIRE_TIMEOUT) as conn:
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
