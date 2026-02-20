from __future__ import annotations

import logging
import asyncio
import os
import random
from dataclasses import dataclass
from typing import Optional

import asyncpg

from repositories.base import create_pool, pool_is_open
from utils.structured_log import log_event

log = logging.getLogger("happy_jumper.database")


class MissingDatabaseColumnError(Exception):
    pass


@dataclass(frozen=True)
class Database:
    pool: asyncpg.Pool


_pool: Optional[asyncpg.Pool] = None
_db: Optional[Database] = None
_initialized_event = asyncio.Event()


def _max_connect_attempts() -> int:
    raw = (os.getenv("DB_CONNECT_MAX_ATTEMPTS", "20") or "20").strip()
    try:
        attempts = int(raw)
    except ValueError:
        attempts = 20
    return max(attempts, 1)


async def init_pool() -> asyncpg.Pool:
    global _pool, _db
    if pool_is_open(_pool):
        return _pool  # type: ignore[return-value]

    max_attempts = _max_connect_attempts()
    base_delay_seconds = 1.0
    max_delay_seconds = 30.0

    for attempt in range(1, max_attempts + 1):
        try:
            _pool = await create_pool()
            _db = Database(pool=_pool)
            _initialized_event.set()
            log.info("Database pool initialized")
            return _pool
        except Exception:
            if attempt >= max_attempts:
                log.exception("Database pool initialization failed after %s attempts", max_attempts)
                raise

            exp_delay = min(max_delay_seconds, base_delay_seconds * (2 ** (attempt - 1)))
            jittered_delay = exp_delay + random.uniform(0, 0.25 * exp_delay)
            log_event(
                log,
                logging.WARNING,
                "db_connect_retry_scheduled",
                action="db_connect",
                result="retry",
                attempt=attempt,
                max_attempts=max_attempts,
                next_sleep_seconds=round(jittered_delay, 2),
            )
            await asyncio.sleep(jittered_delay)

    raise RuntimeError("Database pool initialization exhausted retries")


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
    _initialized_event.clear()


def is_initialized() -> bool:
    return pool_is_open(_pool)


async def wait_until_initialized(timeout: float = 30.0, poll_interval: float = 0.1) -> bool:
    if is_initialized():
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0.0)
    while loop.time() < deadline:
        try:
            await asyncio.wait_for(_initialized_event.wait(), timeout=min(poll_interval, max(deadline - loop.time(), 0.01)))
        except asyncio.TimeoutError:
            pass
        if is_initialized():
            return True
    return is_initialized()


# Backward-compatible alias
init_database = init_pool


def get_database() -> Database:
    global _db
    if _db is None or not pool_is_open(_db.pool):
        _db = Database(pool=get_pool())
    return _db
