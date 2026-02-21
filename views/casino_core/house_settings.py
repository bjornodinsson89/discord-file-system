from __future__ import annotations

import discord

from repositories.users import UsersRepository
from services.casino_core.settings import get_house_config, update_house_config
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from views.casino_core.permissions import ensure_casino_admin


class HouseUserSelect(discord.ui.UserSelect):
    def __init__(self, parent: "HouseSettingsView"):
        super().__init__(placeholder="Select house Discord user", min_values=1, max_values=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        user_id = int(self.values[0].id)
        await update_house_config(self.parent_view.guild_id, {"house_discord_id": user_id})
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.parent_view.guild_id),
            view=HouseSettingsView(self.parent_view.guild_id),
        )


class PayoutsChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: "HouseSettingsView"):
        super().__init__(
            placeholder="Select payouts channel",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        channel_id = int(self.values[0].id)
        await update_house_config(self.parent_view.guild_id, {"payouts_channel_id": channel_id})
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.parent_view.guild_id),
            view=HouseSettingsView(self.parent_view.guild_id),
        )


class CashoutInboxChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent: "HouseSettingsView"):
        super().__init__(
            placeholder="Select cashout inbox channel",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        channel_id = int(self.values[0].id)
        await update_house_config(self.parent_view.guild_id, {"cashout_inbox_channel_id": channel_id})
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.parent_view.guild_id),
            view=HouseSettingsView(self.parent_view.guild_id),
        )


class CasinoAdminRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent: "HouseSettingsView"):
        super().__init__(placeholder="Select casino admin role", min_values=1, max_values=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        role_id = int(self.values[0].id)
        await update_house_config(self.parent_view.guild_id, {"casino_admin_role_id": role_id})
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.parent_view.guild_id),
            view=HouseSettingsView(self.parent_view.guild_id),
        )


class HouseSettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.add_item(HouseUserSelect(self))
        self.add_item(PayoutsChannelSelect(self))
        self.add_item(CashoutInboxChannelSelect(self))
        self.add_item(CasinoAdminRoleSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Use House API Profile", style=discord.ButtonStyle.primary, row=2)
    async def use_api_profile(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(self.guild_id)
        house = get_house_config(settings)
        house_discord_id = house.get("house_discord_id")
        if not house_discord_id:
            await interaction.response.send_message("❌ Set a house Discord user first.", ephemeral=True)
            return

        key_row = await UsersRepository(get_pool()).get_user_api_key(int(house_discord_id))
        torn_user_id = int((key_row or {}).get("torn_user_id") or 0)
        if torn_user_id <= 0:
            await interaction.response.send_message("❌ House must set API key first.", ephemeral=True)
            return

        await update_house_config(self.guild_id, {"house_torn_id": torn_user_id})
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.guild_id),
            view=HouseSettingsView(self.guild_id),
        )

    @discord.ui.button(label="Toggle Casino Enabled", style=discord.ButtonStyle.success, row=2)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        repo = GuildSettingsRepository(get_database())
        row = await repo.get_or_create(self.guild_id)
        await repo.upsert_settings(self.guild_id, casino_enabled=not bool(row.get("casino_enabled")))
        await interaction.response.edit_message(
            embed=await house_settings_embed(self.guild_id),
            view=HouseSettingsView(self.guild_id),
        )


async def house_settings_embed(guild_id: int) -> discord.Embed:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    cfg = get_house_config(settings)
    em = discord.Embed(title="Casino House Settings", color=discord.Color.blurple())
    em.add_field(name="Enabled", value="Yes" if settings.get("casino_enabled") else "No")
    em.add_field(name="House Discord", value=f"<@{cfg.get('house_discord_id')}>" if cfg.get("house_discord_id") else "Not set")
    em.add_field(name="House Torn ID", value=str(cfg.get("house_torn_id") or "Not set"))
    em.add_field(name="Payouts Channel", value=f"<#{cfg.get('payouts_channel_id')}>" if cfg.get("payouts_channel_id") else "Not set", inline=False)
    em.add_field(name="Cashout Inbox", value=f"<#{cfg.get('cashout_inbox_channel_id')}>" if cfg.get("cashout_inbox_channel_id") else "Not set")
    em.add_field(name="Casino Admin Role", value=f"<@&{cfg.get('casino_admin_role_id')}>" if cfg.get("casino_admin_role_id") else "Not set")
    return em
