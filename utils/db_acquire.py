from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def acquire_conn(pool, timeout_seconds: float | int | None):
    """Acquire a DB connection, tolerating pools that do not accept timeout kwarg."""
    try:
        ctx = pool.acquire(timeout=timeout_seconds)
    except TypeError:
        ctx = pool.acquire()

    async with ctx as conn:
        yield conn
