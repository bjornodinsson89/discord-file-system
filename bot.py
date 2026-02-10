"""Happy Jumper Discord bot entrypoint (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import config
from aiohttp import web
from cogs import EXTENSIONS
from cogs.events import bot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
print("STDOUT: bot process started", flush=True)
log = logging.getLogger("happy_jumper")


async def health_server():
    """Dummy HTTP server to keep Railway from killing the container."""
    app = web.Application()
    app.router.add_get('/health', lambda r: web.Response(text='ok'))
    app.router.add_get('/', lambda r: web.Response(text='Happy Jumper Bot Running'))
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Railway injects PORT, or default to 3000
    port = int(os.getenv('PORT', '3000'))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"Health check server started on port {port}")


async def main() -> None:
    config.validate_config()
    log.info("Starting Discord bot service")

    # Start health server FIRST (opens port for Railway health checks)
    asyncio.create_task(health_server())

    for ext in EXTENSIONS:
        try:
            await bot.load_extension(ext)
            log.info("Loaded extension: %s", ext)
        except Exception:
            log.exception("Failed loading extension: %s", ext)

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
