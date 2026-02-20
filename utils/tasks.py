from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field


@dataclass
class TaskSupervisor:
    name: str
    coro_factory: Callable[[], Awaitable[None]]
    restart: bool = True
    backoff: Sequence[float] = (1, 2, 5, 10)
    logger: logging.Logger | None = None
    _task: asyncio.Task | None = field(default=None, init=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def start(self) -> "TaskSupervisor":
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run(), name=f"supervisor:{self.name}")
        return self

    async def _run(self) -> None:
        fail_count = 0
        while not self._stop_event.is_set():
            try:
                await self.coro_factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                fail_count += 1
                logger = self.logger or logging.getLogger("happy_jumper.tasks")
                logger.exception("Supervised task failed: %s", self.name)

                if not self.restart or self._stop_event.is_set():
                    return

                idx = min(fail_count - 1, len(self.backoff) - 1)
                delay = float(self.backoff[idx]) if self.backoff else 1.0
                delay += random.uniform(0.0, min(1.0, max(delay, 0.0) * 0.1))
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=max(delay, 0.0))
                except asyncio.TimeoutError:
                    continue

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def supervise(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
    *,
    restart: bool = True,
    backoff: Sequence[float] = (1, 2, 5, 10),
    logger: logging.Logger | None = None,
) -> TaskSupervisor:
    supervisor = TaskSupervisor(
        name=name,
        coro_factory=coro_factory,
        restart=restart,
        backoff=backoff,
        logger=logger,
    )
    return supervisor.start()
