"""Happy Jumper Discord bot entrypoint (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
import sys

import config
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


async def main() -> None:
    config.validate_config()
    log.info("Starting Discord bot service")

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
