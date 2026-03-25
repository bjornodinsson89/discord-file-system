import asyncio
from pathlib import Path

import asyncpg

from repositories.free_raffle_repo import FreeRaffleRepository


class _FakeConn:
    def __init__(self, columns, *, existing=False, duplicate_on_insert=False):
        self.columns = columns
        self.existing = existing
        self.duplicate_on_insert = duplicate_on_insert
        self.last_update_query = ""
        self.last_insert_query = ""
        self.last_fetchrow_query = ""

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            return [{"column_name": name} for name in self.columns]
        return []

    async def execute(self, query, *args):
        q = " ".join(query.split())
        if q.startswith("UPDATE free_raffle_entries"):
            self.last_update_query = query
            if self.existing:
                self.existing = True
                return "UPDATE 1"
            return "UPDATE 0"
        if q.startswith("INSERT INTO free_raffle_entries"):
            self.last_insert_query = query
            if self.duplicate_on_insert:
                self.duplicate_on_insert = False
                self.existing = True
                raise asyncpg.UniqueViolationError("duplicate")
            self.existing = True
            return "INSERT 0 1"
        return "OK"

    async def fetchrow(self, query, *args):
        self.last_fetchrow_query = query
        return {
            "discord_id": args[1],
            "entry_source": "auto_messages",
            "entry_weight": 1,
            "created_at": None,
        }


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _repo():
    return FreeRaffleRepository.__new__(FreeRaffleRepository)


def test_upsert_and_read_use_both_user_columns_when_both_exist():
    async def _run():
        repo = _repo()
        conn = _FakeConn(columns=["discord_id", "participant_discord_id"])
        repo.acquire = lambda: _AcquireCtx(conn)
        ok = await repo._upsert_entry_with_conn(
            conn,
            raffle_id=1,
            discord_id=99,
            entry_source="auto_messages",
            entry_weight=1,
            dedupe_key="k",
            accumulate=True,
        )
        assert ok is True
        assert "COALESCE(free_raffle_entries.participant_discord_id, $2)" in conn.last_update_query
        await repo.get_entry(1, 99)
        assert (
            "COALESCE(discord_id, participant_discord_id) AS discord_id" in conn.last_fetchrow_query
        )

    asyncio.run(_run())


def test_upsert_and_read_use_discord_id_when_only_discord_column_exists():
    async def _run():
        repo = _repo()
        conn = _FakeConn(columns=["discord_id"])
        repo.acquire = lambda: _AcquireCtx(conn)
        ok = await repo._upsert_entry_with_conn(
            conn,
            raffle_id=1,
            discord_id=99,
            entry_source="auto_messages",
            entry_weight=1,
            dedupe_key="k",
            accumulate=False,
        )
        assert ok is True
        assert "participant_discord_id" not in conn.last_insert_query
        await repo.get_entry(1, 99)
        assert "discord_id AS discord_id" in conn.last_fetchrow_query

    asyncio.run(_run())


def test_upsert_and_read_use_participant_discord_id_when_only_participant_exists():
    async def _run():
        repo = _repo()
        conn = _FakeConn(columns=["participant_discord_id"])
        repo.acquire = lambda: _AcquireCtx(conn)
        ok = await repo._upsert_entry_with_conn(
            conn,
            raffle_id=1,
            discord_id=99,
            entry_source="auto_messages",
            entry_weight=1,
            dedupe_key="k",
            accumulate=True,
        )
        assert ok is True
        assert "participant_discord_id" in conn.last_insert_query
        await repo.get_entry(1, 99)
        assert "participant_discord_id AS discord_id" in conn.last_fetchrow_query

    asyncio.run(_run())


def test_accumulate_upsert_retries_update_when_insert_races():
    async def _run():
        repo = _repo()
        conn = _FakeConn(columns=["discord_id"], existing=False, duplicate_on_insert=True)
        ok = await repo._upsert_entry_with_conn(
            conn,
            raffle_id=2,
            discord_id=33,
            entry_source="auto_messages",
            entry_weight=2,
            dedupe_key="d1",
            accumulate=True,
        )
        assert ok is True
        assert "UPDATE free_raffle_entries" in conn.last_update_query
        assert "INSERT INTO free_raffle_entries" in conn.last_insert_query

    asyncio.run(_run())


def test_increment_auto_entry_progress_path_is_schema_safe_in_source():
    src = Path("repositories/free_raffle_repo.py").read_text(encoding="utf-8")
    assert "_update_entry_with_conn" in src
    assert "_insert_entry_with_conn" in src
    assert "ON CONFLICT (raffle_id, discord_id)" not in src
    assert "ON CONFLICT (raffle_id, participant_discord_id)" not in src


def test_migration_repairs_canonical_uniqueness_and_dedupe_index():
    src = Path("migrations/2026_03_25_fix_free_raffle_entries_user_id_schema_drift.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_user_unique" in src
    assert "ON public.free_raffle_entries (raffle_id, discord_id)" in src
    assert (
        "DROP INDEX IF EXISTS public.idx_free_raffle_entries_raffle_participant_user_unique" in src
    )
    assert "idx_free_raffle_entries_dedupe_key" in src
    assert "WHERE dedupe_key IS NOT NULL" in src
