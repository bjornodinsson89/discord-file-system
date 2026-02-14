from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Optional

import asyncpg

from repositories.base import create_pool, pool_is_open

log = logging.getLogger("happy_jumper.database")


class MissingDatabaseColumnError(Exception):
    pass


_pool: Optional[asyncpg.Pool] = None
_database_manager = SimpleNamespace(pool=None)


async def init_pool() -> asyncpg.Pool:
    global _pool
    if pool_is_open(_pool):
        return _pool  # type: ignore[return-value]
    _pool = await create_pool()
    _database_manager.pool = _pool
    log.info("Database pool initialized")
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
    _database_manager.pool = None


# Backward-compatible alias
init_database = init_pool


def get_database():
    _database_manager.pool = get_pool()
    return _database_manager
