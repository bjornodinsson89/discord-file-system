from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from utils.database import Database

log = logging.getLogger("happy_jumper.advisory_lock")


def lock_key_from_name(name: str) -> int:
    """
    Convert a string to a stable signed 64-bit integer suitable for pg_advisory_lock.
    """
    raw = hashlib.sha256(name.encode("utf-8")).digest()[:8]
    unsigned = int.from_bytes(raw, byteorder="big", signed=False)
    # convert to signed 64-bit range
    if unsigned >= (1 << 63):
        return unsigned - (1 << 64)
    return unsigned


async def run_with_advisory_lock(
    db: Database,
    name: str,
    coro_fn: Callable[[], Awaitable[Any]],
) -> tuple[bool, Any]:
    """
    Attempt to acquire pg advisory lock for this name.
    Returns (acquired, result). If not acquired, result is None.
    """
    key = lock_key_from_name(name)
    start = time.perf_counter()

    async with db.acquire(operation=f"advisory_lock:{name}") as conn:
        acquired = bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", key))
        if not acquired:
            log.debug("advisory_lock.skip name=%s key=%s", name, key)
            return False, None

        try:
            result = await coro_fn()
            return True, result
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
            finally:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log.debug("advisory_lock.release name=%s duration_ms=%s", name, duration_ms)
