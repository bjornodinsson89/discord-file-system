import asyncio
from pathlib import Path

from repositories.free_raffle_repo import FreeRaffleRepository


class _FakeConn:
    def __init__(self, columns):
        self.columns = columns
        self.last_execute_query = ""
        self.last_fetchrow_query = ""

    async def fetch(self, query, *args):
        if "information_schema.columns" in query:
            return [{"column_name": name} for name in self.columns]
        return []

    async def execute(self, query, *args):
        self.last_execute_query = query
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.last_fetchrow_query = query
        return {
            "discord_id": args[1],
            "entry_source": "auto_messages",
            "entry_weight": 1,
            "created_at": None,
        }


def _repo():
    return FreeRaffleRepository.__new__(FreeRaffleRepository)


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


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
        assert "discord_id, participant_discord_id" in conn.last_execute_query
        assert "ON CONFLICT (raffle_id, discord_id)" in conn.last_execute_query
        await repo.get_entry(1, 99)
        assert "COALESCE(discord_id, participant_discord_id) AS discord_id" in conn.last_fetchrow_query

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
        assert "raffle_id, discord_id, entry_source" in conn.last_execute_query
        assert "participant_discord_id" not in conn.last_execute_query
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
        assert "raffle_id, participant_discord_id" in conn.last_execute_query
        assert "ON CONFLICT (raffle_id, participant_discord_id)" in conn.last_execute_query
        await repo.get_entry(1, 99)
        assert "participant_discord_id AS discord_id" in conn.last_fetchrow_query

    asyncio.run(_run())


def test_increment_auto_entry_progress_path_is_schema_safe_in_source():
    src = Path("repositories/free_raffle_repo.py").read_text(encoding="utf-8")
    assert "_get_entry_user_schema" in src
    assert "INSERT INTO free_raffle_entries (" in src


def test_raffle_payment_view_no_longer_uses_uninitialized_entry_id():
    src = Path("views/components.py").read_text(encoding="utf-8")
    assert "self.entry_id" not in src
    assert "verify_entry_payment(int(entry[\"entry_id\"]), manual=True)" in src
