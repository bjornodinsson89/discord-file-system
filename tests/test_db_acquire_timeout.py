import asyncio

import pytest

from utils.database import Database, DatabaseAcquireTimeoutError
import utils.database as db_module


class _HangingPool:
    def __init__(self):
        self.release_calls = 0

    async def acquire(self):
        await asyncio.Future()

    async def release(self, _conn):
        self.release_calls += 1


class _FastPool:
    def __init__(self):
        self.release_calls = 0

    async def acquire(self):
        return object()

    async def release(self, _conn):
        self.release_calls += 1


def test_database_acquire_timeout_logs_and_raises(monkeypatch):
    pool = _HangingPool()
    db = Database(pool=pool)
    logged = {}

    def _fake_log_event(_logger, _level, event, **fields):
        logged["event"] = event
        logged.update(fields)

    monkeypatch.setattr(db_module, "log_event", _fake_log_event)

    async def _run():
        with pytest.raises(DatabaseAcquireTimeoutError):
            async with db.acquire(timeout=0.01, operation="unit_test_timeout"):
                pass

    asyncio.run(_run())

    assert pool.release_calls == 0
    assert logged["event"] == "db_acquire_timeout"
    assert logged["operation"] == "unit_test_timeout"


def test_database_acquire_releases_connection_on_success():
    pool = _FastPool()
    db = Database(pool=pool)

    async def _run():
        async with db.acquire(timeout=0.5, operation="unit_test_success") as conn:
            assert conn is not None

    asyncio.run(_run())

    assert pool.release_calls == 1
