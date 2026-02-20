from __future__ import annotations

import discord


def _missing_perms(perms: list[str]) -> str:
    return f"missing_perms:{'/'.join(perms)}" if perms else "missing_perms"


async def _resolve_channel(guild: discord.Guild, channel_id: int):
    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        return channel, None
    try:
        channel = await guild.fetch_channel(int(channel_id))
        return channel, None
    except discord.NotFound:
        return None, "missing_channel"
    except discord.Forbidden:
        return None, _missing_perms(["ViewChannel"])
    except Exception as exc:
        return None, f"exception:{type(exc).__name__}"


async def delete_message_safe(guild: discord.Guild, channel_id: int | None, message_id: int | None, reason: str, ctx_fields: dict | None = None) -> tuple[bool, str]:
    del ctx_fields
    if not channel_id or not message_id:
        return True, "missing_ids"

    try:
        channel, status = await _resolve_channel(guild, int(channel_id))
        if status:
            return (status == "missing_channel"), status
        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            return True, "not_messageable_channel"

        me = guild.me
        if me:
            perms = channel.permissions_for(me)
            missing = []
            if not perms.view_channel:
                missing.append("ViewChannel")
            if not perms.read_message_history:
                missing.append("ReadMessageHistory")
            if not perms.manage_messages:
                missing.append("ManageMessages")
            if missing:
                return False, _missing_perms(missing)

        msg = await fetch_message(int(message_id))
        await msg.delete(reason=reason)
        return True, "ok"
    except discord.NotFound:
        return True, "already_deleted"
    except discord.Forbidden:
        return False, _missing_perms(["ManageMessages"])
    except (TypeError, AttributeError) as exc:
        return False, f"exception:{type(exc).__name__}"
    except Exception as exc:
        return False, f"exception:{type(exc).__name__}"


async def delete_channel_safe(guild: discord.Guild, channel_id: int | None, reason: str, ctx_fields: dict | None = None) -> tuple[bool, str]:
    del ctx_fields
    if not channel_id:
        return True, "missing_ids"
    try:
        channel, status = await _resolve_channel(guild, int(channel_id))
        if status:
            return (status == "missing_channel"), status
        me = guild.me
        if me:
            perms = channel.permissions_for(me)
            missing = []
            if not perms.view_channel:
                missing.append("ViewChannel")
            if not perms.manage_channels:
                missing.append("ManageChannels")
            if missing:
                return False, _missing_perms(missing)
        await channel.delete(reason=reason)
        return True, "ok"
    except discord.NotFound:
        return True, "already_deleted"
    except discord.Forbidden:
        return False, _missing_perms(["ManageChannels"])
    except (TypeError, AttributeError) as exc:
        return False, f"exception:{type(exc).__name__}"
    except Exception as exc:
        return False, f"exception:{type(exc).__name__}"
