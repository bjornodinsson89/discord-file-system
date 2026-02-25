from __future__ import annotations

import logging

import discord

from services.logging_utils import log_event

log = logging.getLogger("happy_jumper.cleanup")


def _missing_perms(perms: list[str]) -> str:
    return f"missing_perms:{'/'.join(perms)}" if perms else "missing_perms"


async def _delete_compat(obj: object, *, reason: str) -> None:
    delete = getattr(obj, "delete", None)
    if not callable(delete):
        raise TypeError(f"Object {type(obj).__name__} has no callable delete()")
    try:
        await delete(reason=reason)
    except TypeError:
        await delete()


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
    fields = dict(ctx_fields or {})
    fields.setdefault("guild_id", getattr(guild, "id", None))
    fields.setdefault("action", "delete_message")

    if not channel_id or not message_id:
        log_event(log, logging.INFO, "cleanup.delete_message", result="missing_ids", channel_id=channel_id, message_id=message_id, **fields)
        return True, "missing_ids"

    try:
        channel, status = await _resolve_channel(guild, int(channel_id))
        if status:
            ok = status in {"missing_channel"}
            log_event(log, logging.INFO if ok else logging.WARNING, "cleanup.delete_message", result=status, channel_id=channel_id, message_id=message_id, **fields)
            return ok, status

        fetch_message = getattr(channel, "fetch_message", None)
        if not callable(fetch_message):
            log_event(log, logging.WARNING, "cleanup.delete_message", result="not_messageable_channel", channel_id=channel_id, message_id=message_id, **fields)
            return False, "not_messageable_channel"

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
                status = _missing_perms(missing)
                log_event(log, logging.WARNING, "cleanup.delete_message", result=status, channel_id=channel_id, message_id=message_id, **fields)
                return False, status

        msg = await fetch_message(int(message_id))
        await _delete_compat(msg, reason=reason)
        log_event(log, logging.INFO, "cleanup.delete_message", result="ok", channel_id=channel_id, message_id=message_id, **fields)
        return True, "ok"
    except discord.NotFound:
        log_event(log, logging.INFO, "cleanup.delete_message", result="already_deleted", channel_id=channel_id, message_id=message_id, **fields)
        return True, "already_deleted"
    except discord.Forbidden:
        status = _missing_perms(["ManageMessages"])
        log_event(log, logging.WARNING, "cleanup.delete_message", result=status, channel_id=channel_id, message_id=message_id, **fields)
        return False, status
    except Exception as exc:
        status = f"exception:{type(exc).__name__}"
        log_event(log, logging.ERROR, "cleanup.delete_message", result=status, error_type=type(exc).__name__, channel_id=channel_id, message_id=message_id, exc_info=True, **fields)
        return False, status


async def delete_channel_safe(guild: discord.Guild, channel_id: int | None, reason: str, ctx_fields: dict | None = None) -> tuple[bool, str]:
    fields = dict(ctx_fields or {})
    fields.setdefault("guild_id", getattr(guild, "id", None))
    fields.setdefault("action", "delete_channel")

    if not channel_id:
        log_event(log, logging.INFO, "cleanup.delete_channel", result="missing_ids", channel_id=channel_id, **fields)
        return True, "missing_ids"
    try:
        channel, status = await _resolve_channel(guild, int(channel_id))
        if status:
            ok = status in {"missing_channel"}
            log_event(log, logging.INFO if ok else logging.WARNING, "cleanup.delete_channel", result=status, channel_id=channel_id, **fields)
            return ok, status
        me = guild.me
        if me:
            perms = channel.permissions_for(me)
            missing = []
            if not perms.view_channel:
                missing.append("ViewChannel")
            if not perms.manage_channels:
                missing.append("ManageChannels")
            if missing:
                status = _missing_perms(missing)
                log_event(log, logging.WARNING, "cleanup.delete_channel", result=status, channel_id=channel_id, **fields)
                return False, status
        await _delete_compat(channel, reason=reason)
        log_event(log, logging.INFO, "cleanup.delete_channel", result="ok", channel_id=channel_id, **fields)
        return True, "ok"
    except discord.NotFound:
        log_event(log, logging.INFO, "cleanup.delete_channel", result="already_deleted", channel_id=channel_id, **fields)
        return True, "already_deleted"
    except discord.Forbidden:
        status = _missing_perms(["ManageChannels"])
        log_event(log, logging.WARNING, "cleanup.delete_channel", result=status, channel_id=channel_id, **fields)
        return False, status
    except Exception as exc:
        status = f"exception:{type(exc).__name__}"
        log_event(log, logging.ERROR, "cleanup.delete_channel", result=status, error_type=type(exc).__name__, channel_id=channel_id, exc_info=True, **fields)
        return False, status
