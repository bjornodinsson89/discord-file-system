"""Boot smoke helper: invokes setup_hook without starting Discord connection."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs import events


async def main() -> None:
    if os.getenv("BOOT_SMOKE_SKIP", ""):
        print("BOOT_SMOKE_SKIP set; skipping setup_hook smoke")
        return
    await events.setup_hook()
    print("setup_hook completed")


if __name__ == "__main__":
    asyncio.run(main())
