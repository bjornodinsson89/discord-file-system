from __future__ import annotations

from typing import Any, Optional

import discord


GuildSettings = dict[str, Any]


def has_role(member: discord.Member, role_id: Optional[int]) -> bool:
    if role_id is None:
        return False
    try:
        wanted = int(role_id)
    except (TypeError, ValueError):
        return False
    return any(int(role.id) == wanted for role in member.roles)


def can_manage_paid_raffles(member: discord.Member, settings: GuildSettings) -> bool:
    """Return True when the member has paid-raffle management access.

    Access is granted if the member is a Discord administrator or has the
    configured raffle host role.
    """
    if member.guild_permissions.administrator:
        return True

    raffle_host_role_id_raw = settings.get("raffle_host_role_id")
    raffle_host_role_id: Optional[int]
    try:
        raffle_host_role_id = (
            int(raffle_host_role_id_raw) if raffle_host_role_id_raw not in (None, "") else None
        )
    except (TypeError, ValueError):
        raffle_host_role_id = None
    return has_role(member, raffle_host_role_id)
