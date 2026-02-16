from __future__ import annotations

import discord

from repositories.users import UsersRepository


async def has_api_key(db, discord_id: int) -> bool:
    """Return True when the user has a stored encrypted Torn API key."""
    key_row = await UsersRepository(db.pool).get_user_api_key(int(discord_id))
    encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
    return bool(encrypted_key)


async def require_api_key(interaction: discord.Interaction, db, action_label: str) -> bool:
    """Guard Torn-dependent interactions with a uniform missing-key response."""
    discord_id = int(interaction.user.id)
    _guild_id = int(interaction.guild_id) if interaction.guild_id else None

    if await has_api_key(db, discord_id):
        return True

    message = (
        f"❗ You need to register your Torn API key before you can {action_label}.\n"
        "Run `/set api key` to register it.\n"
        "This is required to verify payments and fetch your energy/cooldowns."
    )

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
    return False
