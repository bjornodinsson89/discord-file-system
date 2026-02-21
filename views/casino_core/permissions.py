from __future__ import annotations

import discord

from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database


async def is_casino_admin(interaction: discord.Interaction, guild_id: int | None) -> bool:
    if not guild_id:
        return False
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True

    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    house = get_house_config(settings)
    admin_role_id = int(house.get("casino_admin_role_id") or 0)
    if not admin_role_id:
        return False
    role_ids = {int(r.id) for r in getattr(member, "roles", [])}
    return admin_role_id in role_ids


async def ensure_casino_admin(interaction: discord.Interaction, guild_id: int | None) -> bool:
    if await is_casino_admin(interaction, guild_id):
        return True
    if interaction.response.is_done():
        await interaction.followup.send("❌ Admin only.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    return False
