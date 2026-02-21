import asyncio

from utils.db_acquire import acquire_conn


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _PoolWithoutTimeoutAcquire:
    def __init__(self):
        self.conn = object()
        self.calls: list[str] = []

    def acquire(self):
        self.calls.append("acquire_without_timeout")
        return _AcquireCtx(self.conn)


def test_acquire_conn_falls_back_when_pool_acquire_has_no_timeout_kwarg():
    pool = _PoolWithoutTimeoutAcquire()

    async def _run():
        async with acquire_conn(pool, 0.5) as conn:
            assert conn is pool.conn

    asyncio.run(_run())

    assert pool.calls == ["acquire_without_timeout"]
