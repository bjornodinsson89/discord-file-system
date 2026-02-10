from __future__ import annotations

import logging
from typing import Optional

import asyncpg

import config

log = logging.getLogger("happy_jumper.repositories")


class RepositoryBase:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool


def pool_is_open(pool: Optional[asyncpg.Pool]) -> bool:
    if pool is None:
        return False
    is_closing = getattr(pool, "is_closing", None)
    if callable(is_closing):
        return not bool(is_closing())
    closed = getattr(pool, "_closed", None)
    if closed is not None:
        return not bool(closed)
    return True


async def create_pool() -> asyncpg.Pool:
    ssl_mode = config.get_db_ssl_config()
    log.info("Initializing DB pool (ssl_mode=%s)", (config.DB_SSL or "disable").strip().lower())
    pool = await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        ssl=ssl_mode,
        min_size=2,
        max_size=10,
        command_timeout=60,
        statement_cache_size=0,
    )
    return pool
