from __future__ import annotations

import logging
from typing import Any

import discord

log = logging.getLogger("happy_jumper.discord_safe_send")


async def safe_send_channel(
    bot_or_guild: discord.Client | discord.Guild,
    channel_id: int | None,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> bool:
    if channel_id is None:
        return False

    try:
        resolved_channel_id = int(channel_id)
    except (TypeError, ValueError):
        return False

    if resolved_channel_id <= 0:
        return False

    channel: Any = None
    try:
        channel = bot_or_guild.get_channel(resolved_channel_id)
    except Exception:
        channel = None

    if channel is None:
        try:
            channel = await bot_or_guild.fetch_channel(resolved_channel_id)
        except Exception:
            return False

    if not hasattr(channel, "send"):
        return False

    try:
        await channel.send(content=content, embed=embed, view=view, allowed_mentions=allowed_mentions)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.warning("safe_send_channel failed channel_id=%s", resolved_channel_id, exc_info=True)
        return False
    return True
