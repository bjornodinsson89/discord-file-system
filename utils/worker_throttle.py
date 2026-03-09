from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import config

log = logging.getLogger("happy_jumper.worker_throttle")

_DB_HEAVY_WORKER_GATE = asyncio.Semaphore(config.DB_HEAVY_WORKER_CONCURRENCY)


@asynccontextmanager
async def db_heavy_worker_slot(worker_name: str) -> AsyncIterator[None]:
    await _DB_HEAVY_WORKER_GATE.acquire()
    try:
        yield
    finally:
        _DB_HEAVY_WORKER_GATE.release()


async def sleep_startup_jitter(worker_name: str) -> None:
    max_jitter = float(max(config.DB_WORKER_STARTUP_JITTER_SECONDS, 0))
    if max_jitter <= 0:
        return
    delay = random.uniform(0.0, max_jitter)
    if delay >= 0.1:
        log.debug("worker startup jitter worker=%s delay=%.2fs", worker_name, delay)
    await asyncio.sleep(delay)
