"""Happy Jumper Discord bot entrypoint (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import config
from aiohttp import web
from cogs.events import bot
from utils.database import get_pool, is_initialized as db_is_initialized


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
            pool = get_pool()
            async with pool.acquire() as conn:
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


async def main() -> None:
    config.validate_config()
    log.info("Starting Discord bot service")

    # Start health server FIRST (opens port for Railway health checks)
    asyncio.create_task(health_server())

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
