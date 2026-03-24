"""Happy Jumper Discord bot entrypoint (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys

import discord
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

DISCORD_LOGIN_BACKOFF_BASE_SECONDS = 30.0
DISCORD_LOGIN_BACKOFF_MAX_SECONDS = 900.0
DISCORD_LOGIN_BACKOFF_JITTER_SECONDS = 5.0

_discord_startup_state = "starting"


def _set_discord_startup_state(state: str) -> None:
    global _discord_startup_state
    _discord_startup_state = state


def _summarize_startup_error(exc: BaseException, *, limit: int = 220) -> str:
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return f"{collapsed[: limit - 3]}..."
    return collapsed


def _is_discord_rate_limited_error(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    lower = str(exc).lower()
    if status == 429 or code == 429:
        return True
    if "cloudflare" in lower and "1015" in lower:
        return True
    if "/users/@me" in lower and "rate limit" in lower:
        return True
    if "too many requests" in lower and "discord" in lower:
        return True
    return "rate limited" in lower and "login" in lower


def _is_retryable_network_startup_error(exc: BaseException) -> bool:
    retryable_types = (
        asyncio.TimeoutError,
        OSError,
        ConnectionError,
        discord.GatewayNotFound,
        discord.DiscordServerError,
    )
    if isinstance(exc, retryable_types):
        return True
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        return status is not None and int(status) >= 500
    return False


def _compute_backoff_delay(
    failure_count: int,
    *,
    base_delay: float = DISCORD_LOGIN_BACKOFF_BASE_SECONDS,
    max_delay: float = DISCORD_LOGIN_BACKOFF_MAX_SECONDS,
    jitter_max: float = DISCORD_LOGIN_BACKOFF_JITTER_SECONDS,
) -> float:
    exponent = max(failure_count - 1, 0)
    core_delay = min(max_delay, base_delay * (2**exponent))
    jitter = random.uniform(0.0, max(jitter_max, 0.0))
    return min(max_delay, core_delay + jitter)


async def _safe_close_bot(client: discord.Client) -> None:
    try:
        await client.close()
    except Exception as exc:
        log.warning("Error while closing bot after failed startup attempt: %s", exc)


async def _run_single_start_attempt(
    client: discord.Client,
    *,
    token: str,
    stop_event: asyncio.Event,
) -> None:
    bot_task = asyncio.create_task(client.start(token), name="discord_bot")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown_wait")
    try:
        done, pending = await asyncio.wait(
            {bot_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

        if stop_task in done:
            if not bot_task.done():
                bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass
            return

        await bot_task
    finally:
        if not stop_task.done():
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass


async def run_discord_startup_loop(
    client: discord.Client,
    *,
    token: str,
    stop_event: asyncio.Event,
) -> None:
    if not token or not str(token).strip():
        raise RuntimeError("DISCORD_TOKEN is missing or empty.")

    rate_limit_failures = 0
    transient_failures = 0
    attempt = 0
    while not stop_event.is_set():
        attempt += 1
        _set_discord_startup_state("starting")
        try:
            await _run_single_start_attempt(client, token=token, stop_event=stop_event)
            rate_limit_failures = 0
            transient_failures = 0
            if stop_event.is_set():
                _set_discord_startup_state("stopped")
            else:
                _set_discord_startup_state("disconnected")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, discord.LoginFailure):
                _set_discord_startup_state("failed")
                raise

            retry_kind = None
            if _is_discord_rate_limited_error(exc):
                retry_kind = "rate_limited"
                rate_limit_failures += 1
                transient_failures = 0
                failure_count = rate_limit_failures
            elif _is_retryable_network_startup_error(exc):
                retry_kind = "transient_network"
                transient_failures += 1
                rate_limit_failures = 0
                failure_count = transient_failures
            else:
                _set_discord_startup_state("failed")
                raise

            await _safe_close_bot(client)
            delay = _compute_backoff_delay(failure_count)
            _set_discord_startup_state("backing_off")
            log.warning(
                "Discord startup %s (attempt=%s): %s. Retrying in %.1f seconds.",
                retry_kind,
                attempt,
                _summarize_startup_error(exc),
                delay,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue


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
            "discord": _discord_startup_state,
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
        await run_discord_startup_loop(bot, token=config.DISCORD_TOKEN, stop_event=stop_event)
    finally:
        await _safe_close_bot(bot)
        await health_supervisor.stop()
        await shutdown_status_panel_tasks()
        await shutdown_jump_monitor()


if __name__ == "__main__":
    asyncio.run(main())
