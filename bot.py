"""Happy Jumper Discord bot entrypoint (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import config
from aiohttp import web
from cogs.events import bot
from services.jump_monitor import shutdown_jump_monitor
from utils.database import get_database, is_initialized as db_is_initialized
from utils.tasks import supervise
from views.components import shutdown_status_panel_tasks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
print("STDOUT: bot process started", flush=True)
log = logging.getLogger("happy_jumper")


def _safe_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    if stripped == "":
        return default
    try:
        return int(stripped)
    except (TypeError, ValueError):
        return default


async def health_server():
    """HTTP server with DB-aware health checks."""

    async def _health(_request: web.Request) -> web.Response:
        if not db_is_initialized():
            return web.Response(status=503, text='db_not_initialized')
        try:
            db = get_database()
            async with db.acquire(timeout=5, operation="health_check") as conn:
                await conn.execute('SELECT 1')
        except Exception as exc:
            log.warning("Health check DB probe failed: %s", exc)
            return web.Response(status=503, text='db_unhealthy')
        return web.Response(text='ok')

    app = web.Application()
    app.router.add_get('/health', _health)
    app.router.add_get('/', lambda r: web.Response(text='Happy Jumper Bot Running'))
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Railway injects PORT, or default to 3000
    port = _safe_int_env('PORT', 3000)
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Health check server started on port {port}")

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    config.validate_config()
    log.info("Starting Discord bot service")

    # Start health server FIRST (opens port for Railway health checks)
    health_supervisor = supervise(
        name="health_server",
        coro_factory=health_server,
        restart=True,
        backoff=(1, 2, 5, 10),
        logger=log,
    )

    stop_event = asyncio.Event()

    def _handle_shutdown_signal(sig: signal.Signals) -> None:
        log.info("Received %s, initiating graceful shutdown", sig.name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_shutdown_signal, sig)
        except NotImplementedError:
            pass

    try:
        async with bot:
            bot_task = asyncio.create_task(bot.start(config.DISCORD_TOKEN), name="discord_bot")
            stop_task = asyncio.create_task(stop_event.wait(), name="shutdown_wait")
            done, pending = await asyncio.wait(
                {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if stop_task in done and not bot_task.done():
                await bot.close()
                await bot_task
            elif bot_task in done:
                await bot_task
    finally:
        await health_supervisor.stop()
        await shutdown_status_panel_tasks()
        await shutdown_jump_monitor()


if __name__ == "__main__":
    asyncio.run(main())
