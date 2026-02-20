from __future__ import annotations

import discord


def _check_channel(guild: discord.Guild, me: discord.Member, channel_id: int | None, required: list[str]) -> dict:
    if not channel_id:
        return {"channel_id": None, "exists": False, "type": None, "missing_permissions": ["channel_not_configured"]}
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return {"channel_id": int(channel_id), "exists": False, "type": None, "missing_permissions": ["missing_channel"]}
    perms = channel.permissions_for(me)
    missing = [p for p in required if not getattr(perms, p.lower(), False)]
    return {
        "channel_id": int(channel_id),
        "exists": True,
        "type": str(channel.type),
        "missing_permissions": missing,
    }


def validate_99k_permissions(guild: discord.Guild, bot_user: discord.abc.User, *, signup_channel_id: int | None, announce_channel_id: int | None, private_category_id: int | None) -> dict:
    me = guild.get_member(bot_user.id)
    if me is None:
        return {"guild_id": guild.id, "resolved_bot_member": False, "channels": {}}
    base = ["view_channel", "send_messages", "embed_links", "read_message_history", "manage_messages"]
    channels = {
        "signup_channel": _check_channel(guild, me, signup_channel_id, base),
        "announce_channel": _check_channel(guild, me, announce_channel_id, base),
        "private_category": _check_channel(guild, me, private_category_id, ["view_channel", "manage_channels"]),
    }
    return {"guild_id": guild.id, "resolved_bot_member": True, "channels": channels}
