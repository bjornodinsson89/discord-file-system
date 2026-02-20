import sys
import types

if "certifi" not in sys.modules:
    sys.modules["certifi"] = types.SimpleNamespace(where=lambda: "/etc/ssl/certs/ca-certificates.crt")

import asyncio

import utils.database as db_module


class _DummyPool:
    def is_closing(self):
        return False

    async def close(self):
        return None


def test_init_pool_retries_then_succeeds(monkeypatch):
    calls = {"count": 0}

    async def _fake_create_pool():
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError("temporary db down")
        return _DummyPool()

    async def _fast_sleep(_seconds: float):
        return None

    asyncio.run(db_module.close_pool())
    monkeypatch.setenv("DB_CONNECT_MAX_ATTEMPTS", "5")
    monkeypatch.setattr(db_module, "create_pool", _fake_create_pool)
    monkeypatch.setattr(db_module.random, "uniform", lambda _a, _b: 0.0)
    monkeypatch.setattr(db_module.asyncio, "sleep", _fast_sleep)

    pool = asyncio.run(db_module.init_pool())

    assert isinstance(pool, _DummyPool)
    assert calls["count"] == 3
    assert db_module.is_initialized() is True

    asyncio.run(db_module.close_pool())
