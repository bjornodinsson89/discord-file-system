from __future__ import annotations

import discord
from discord import app_commands

from setup_panel import has_setup_permission
from utils import GuildSettingsRepository, get_database


class CommandAccessError(app_commands.CheckFailure):
    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


def validate_interaction_context(interaction: discord.Interaction) -> discord.Member:
    """Validate that command interaction is from a real guild member context."""
    guild = getattr(interaction, "guild", None)
    guild_id = getattr(interaction, "guild_id", None) or getattr(guild, "id", None)
    if guild is None or guild_id is None:
        raise CommandAccessError("This command can only be used in a server.")

    member = interaction.user
    if member is None or not hasattr(member, "roles") or not hasattr(member, "guild_permissions"):
        raise CommandAccessError("Unable to resolve your server membership for this command.")

    if getattr(member, "bot", False):
        raise CommandAccessError("Bot accounts cannot run this command.")

    return member


def require_command_access(
    *,
    include_configured_admin_roles: bool = False,
    allow_manage_guild: bool = False,
    required_role_setting_keys: tuple[str, ...] = (),
    failure_message: str = "You do not have permission to use this command.",
):
    async def predicate(interaction: discord.Interaction) -> bool:
        member = validate_interaction_context(interaction)

        if member.id == interaction.guild.owner_id or member.guild_permissions.administrator:
            return True

        if allow_manage_guild and member.guild_permissions.manage_guild:
            return True

        if include_configured_admin_roles or required_role_setting_keys:
            settings = await GuildSettingsRepository(get_database()).get_or_create(
                interaction.guild.id
            )
            if include_configured_admin_roles:
                allowed = has_setup_permission(
                    member_id=member.id,
                    guild_owner_id=interaction.guild.owner_id,
                    is_administrator=member.guild_permissions.administrator,
                    can_manage_guild=member.guild_permissions.manage_guild,
                    member_role_ids={role.id for role in member.roles},
                    admin_role_ids=GuildSettingsRepository.resolve_admin_role_ids(settings),
                )
                if allowed:
                    return True

            member_role_ids = {int(role.id) for role in member.roles}
            for key in required_role_setting_keys:
                role_id = int(settings.get(key) or 0)
                if role_id > 0 and role_id in member_role_ids:
                    return True

        raise CommandAccessError(failure_message)

    return app_commands.check(predicate)


def has_role_hierarchy_access(
    *, guild: discord.Guild, actor: discord.Member, target_role: discord.Role
) -> bool:
    if actor.id == guild.owner_id:
        return True
    return actor.top_role > target_role
