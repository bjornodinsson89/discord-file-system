from __future__ import annotations

from typing import Any, Optional

import discord

from utils.guild_settings_repository import GuildSettingsRepository


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

    Access is granted if the member is a Discord administrator, has any configured
    setup admin role, or has the configured paid-raffle secondary role.
    """
    if member.guild_permissions.administrator:
        return True

    admin_role_ids = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
    member_role_ids = {int(role.id) for role in member.roles}
    if bool(member_role_ids & admin_role_ids):
        return True

    paid_role_id_raw = settings.get("paid_raffle_admin_role_id")
    paid_role_id: Optional[int]
    try:
        paid_role_id = int(paid_role_id_raw) if paid_role_id_raw not in (None, "") else None
    except (TypeError, ValueError):
        paid_role_id = None
    return has_role(member, paid_role_id)
