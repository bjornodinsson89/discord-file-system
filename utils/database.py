from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

from repositories.base import create_pool, pool_is_open

log = logging.getLogger("happy_jumper.database")


class MissingDatabaseColumnError(Exception):
    pass


@dataclass(frozen=True)
class Database:
    pool: asyncpg.Pool


_pool: Optional[asyncpg.Pool] = None
_db: Optional[Database] = None


async def init_pool() -> asyncpg.Pool:
    global _pool, _db
    if pool_is_open(_pool):
        return _pool  # type: ignore[return-value]
    _pool = await create_pool()
    _db = Database(pool=_pool)
    log.info("Database pool initialized")
    return _pool


def get_pool() -> asyncpg.Pool:
    if not pool_is_open(_pool):
        raise RuntimeError("Database pool is not initialized. Call init_pool() first.")
    return _pool  # type: ignore[return-value]


async def close_pool() -> None:
    global _pool, _db
    if _pool is not None and pool_is_open(_pool):
        await _pool.close()
    _pool = None
    _db = None


# Backward-compatible alias
init_database = init_pool


def get_database() -> Database:
    global _db
    if _db is None or not pool_is_open(_db.pool):
        _db = Database(pool=get_pool())
    return _db
