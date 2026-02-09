from __future__ import annotations

from typing import Any

import discord


async def resolve_guild_channel(interaction: discord.Interaction, selected: Any) -> discord.abc.GuildChannel | None:
    """Resolve lightweight app-command channel selections to real guild channels."""
    if isinstance(selected, discord.abc.GuildChannel):
        return selected

    guild = interaction.guild
    channel_id = getattr(selected, "id", None)
    if guild is None or channel_id is None:
        return None

    resolved = guild.get_channel(channel_id)
    if resolved is not None and hasattr(resolved, "permissions_for"):
        return resolved

    bot = getattr(interaction, "client", None)
    if bot is None:
        return None

    try:
        fetched = await bot.fetch_channel(channel_id)
    except Exception:
        return None

    if hasattr(fetched, "permissions_for"):
        return fetched

    return None
