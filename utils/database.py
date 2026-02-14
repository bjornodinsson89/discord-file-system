from __future__ import annotations

from typing import Optional

import asyncpg

from repositories.base import create_pool, pool_is_open

_pool: Optional[asyncpg.Pool] = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if pool_is_open(_pool):
        return _pool  # type: ignore[return-value]
    _pool = await create_pool()
    return _pool


def get_pool() -> asyncpg.Pool:
    if not pool_is_open(_pool):
        raise RuntimeError("Database pool is not initialized. Call init_pool() first.")
    return _pool  # type: ignore[return-value]


async def close_pool() -> None:
    global _pool
    if _pool is not None and pool_is_open(_pool):
        await _pool.close()
    _pool = None


# Backward-compatible aliases
init_database = init_pool

def get_database():
    from types import SimpleNamespace
    return SimpleNamespace(pool=get_pool())
