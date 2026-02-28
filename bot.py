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
from services.jump_monitor import get_jump_monitor, shutdown_jump_monitor
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
        version = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_SHA") or "unknown"
        db_status = "ok"
        status_code = 200
        if not db_is_initialized():
            db_status = "degraded"
            status_code = 503
        else:
            try:
                db = get_database()
                async with db.acquire(timeout=3, operation="health_check") as conn:
                    await conn.execute("SELECT 1")
            except Exception as exc:
                log.warning("Health check DB probe failed: %s", exc)
                db_status = "degraded"
                status_code = 503

        payload = {
            "status": "ok",
            "version": version,
            "db": db_status,
            "worker_status": get_jump_monitor().get_worker_status(),
        }
        return web.json_response(payload, status=status_code)

    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/", lambda r: web.Response(text="Happy Jumper Bot Running"))

    runner = web.AppRunner(app)
    await runner.setup()

    # Railway injects PORT, or default to 3000
    port = _safe_int_env("PORT", 3000)
    site = web.TCPSite(runner, "0.0.0.0", port)
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
                {bot_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel whichever task did not finish
            for task in pending:
                task.cancel()

            # If we were asked to stop, gracefully close and suppress expected cancellation noise
            if stop_task in done:
                if not bot_task.done():
                    try:
                        await bot.close()
                    except Exception as exc:
                        # Close errors during shutdown should not prevent exit
                        log.warning("Error while closing bot during shutdown: %s", exc)

                    # Ensure the bot task does not keep running
                    if not bot_task.done():
                        bot_task.cancel()

                try:
                    await bot_task
                except asyncio.CancelledError:
                    # Normal during shutdown (connect/start gets cancelled)
                    pass
                except Exception as exc:
                    # During shutdown, treat unexpected end as warning (not crash spam)
                    log.warning("Bot task ended during shutdown: %s", exc)
            else:
                # Bot task finished first: propagate real failures
                try:
                    await bot_task
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                        try:
                            await stop_task
                        except asyncio.CancelledError:
                            pass
    finally:
        await health_supervisor.stop()
        await shutdown_status_panel_tasks()
        await shutdown_jump_monitor()


if __name__ == "__main__":
    asyncio.run(main())
