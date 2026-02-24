from __future__ import annotations

import discord

from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config, update_house_config
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from views.casino_core.permissions import ensure_casino_admin


class _HouseUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select house Discord user", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, HouseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        house_user = int(self.values[0].id)
        await update_house_config(view.guild_id, {"house_discord_id": house_user})
        await interaction.response.edit_message(embed=await house_settings_embed(view.guild_id), view=view)


class _PayoutProofChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select payout proof channel (optional)",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, HouseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        channel_id = int(self.values[0].id)
        await update_house_config(view.guild_id, {"payout_proof_channel_id": channel_id})
        await interaction.response.edit_message(embed=await house_settings_embed(view.guild_id), view=view)


class _BigWinsChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="Select big wins channel (optional)",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, HouseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        channel_id = int(self.values[0].id)
        await update_house_config(view.guild_id, {"big_wins_channel_id": channel_id})
        await interaction.response.edit_message(embed=await house_settings_embed(view.guild_id), view=view)


class _AdminRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Select casino admin role", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, HouseSettingsView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        role_id = int(self.values[0].id)
        await update_house_config(view.guild_id, {"casino_admin_role_id": role_id})
        await interaction.response.edit_message(embed=await house_settings_embed(view.guild_id), view=view)


class HouseSettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.users_repo = UsersRepository(get_pool())
        self.add_item(_HouseUserSelect())
        self.add_item(_PayoutProofChannelSelect())
        self.add_item(_BigWinsChannelSelect())
        self.add_item(_AdminRoleSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Use House API Profile", style=discord.ButtonStyle.primary, row=3)
    async def use_house_api_profile(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(self.guild_id)
        cfg = get_house_config(settings)
        house_discord_id = int(cfg.get("house_discord_id") or 0)
        if not house_discord_id:
            await interaction.response.send_message("❌ Set House Discord user first.", ephemeral=True)
            return
        house_row = await self.users_repo.get_user_api_key(house_discord_id)
        house_torn_id = int((house_row or {}).get("torn_user_id") or 0)
        if not house_torn_id:
            await interaction.response.send_message("❌ House must set API key first.", ephemeral=True)
            return
        await update_house_config(self.guild_id, {"house_torn_id": house_torn_id})
        await interaction.response.edit_message(embed=await house_settings_embed(self.guild_id), view=self)

    @discord.ui.button(label="Toggle Casino Enabled", style=discord.ButtonStyle.success, row=3)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        repo = GuildSettingsRepository(get_database())
        row = await repo.get_or_create(self.guild_id)
        await repo.upsert_settings(self.guild_id, casino_enabled=not bool(row.get("casino_enabled")))
        await interaction.response.edit_message(embed=await house_settings_embed(self.guild_id), view=self)


async def house_settings_embed(guild_id: int) -> discord.Embed:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    cfg = get_house_config(settings)
    em = discord.Embed(title="Casino House Settings", color=discord.Color.blurple())
    em.add_field(name="Enabled", value="Yes" if settings.get("casino_enabled") else "No")
    em.add_field(name="House User", value=f"<@{cfg.get('house_discord_id')}>" if cfg.get("house_discord_id") else "Not set")
    em.add_field(name="House Torn ID", value=str(cfg.get("house_torn_id") or "Not set"))
    em.add_field(name="Payout Proof Channel (optional)", value=f"<#{cfg.get('payout_proof_channel_id')}>" if cfg.get("payout_proof_channel_id") else "Not set", inline=False)
    em.add_field(name="Big Wins Channel (optional)", value=f"<#{cfg.get('big_wins_channel_id')}>" if cfg.get("big_wins_channel_id") else "Not set")
    em.add_field(name="Casino Admin Role", value=f"<@&{cfg.get('casino_admin_role_id')}>" if cfg.get("casino_admin_role_id") else "Not set")
    return em
