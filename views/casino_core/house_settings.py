from __future__ import annotations

import discord

from services.casino_core.settings import get_house_config, update_house_config
from utils import GuildSettingsRepository, get_database
from views.casino_core.permissions import ensure_casino_admin
from views.casino_core.shared import parse_snowflake


class _SetValueModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Value", required=False, max_length=100)

    def __init__(self, title: str, key: str, guild_id: int):
        super().__init__(title=title)
        self.key = key
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        v = parse_snowflake(str(self.value.value))
        await update_house_config(self.guild_id, {self.key: v})
        await interaction.response.send_message("✅ Saved.", ephemeral=True)


class HouseSettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="House Discord User", style=discord.ButtonStyle.primary)
    async def set_house_discord(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(_SetValueModal("Set House Discord User", "house_discord_id", self.guild_id))

    @discord.ui.button(label="House Torn ID", style=discord.ButtonStyle.primary)
    async def set_house_torn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(_SetValueModal("Set House Torn ID", "house_torn_id", self.guild_id))

    @discord.ui.button(label="Payouts Channel", style=discord.ButtonStyle.secondary)
    async def set_payouts_channel(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(_SetValueModal("Set Payouts Channel", "payouts_channel_id", self.guild_id))

    @discord.ui.button(label="Cashout Inbox Channel", style=discord.ButtonStyle.secondary)
    async def set_cashout_inbox(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(_SetValueModal("Set Cashout Inbox Channel", "cashout_inbox_channel_id", self.guild_id))

    @discord.ui.button(label="Casino Admin Role", style=discord.ButtonStyle.secondary)
    async def set_admin_role(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(_SetValueModal("Set Casino Admin Role", "casino_admin_role_id", self.guild_id))

    @discord.ui.button(label="Toggle Casino Enabled", style=discord.ButtonStyle.success)
    async def toggle_enabled(self, interaction: discord.Interaction, _: discord.ui.Button):
        repo = GuildSettingsRepository(get_database())
        row = await repo.get_or_create(self.guild_id)
        await repo.upsert_settings(self.guild_id, casino_enabled=not bool(row.get("casino_enabled")))
        await interaction.response.send_message("✅ Toggled casino enabled.", ephemeral=True)


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
