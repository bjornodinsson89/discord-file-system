from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.casino_core.registry import get_game_registry
from services.casino_games.slots import CasinoSlotsService, SlotsError
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from views.casino_core import (
    AdminLedgerView,
    CasinoHomeView,
    CashoutRequestModal,
    DepositPanelView,
    HouseSettingsView,
    casino_home_embed,
    deposit_panel_embed,
    house_settings_embed,
)
from views.casino_core.game_settings_panels import (
    DiceSettingsView,
    RouletteSettingsView,
    SlotsSettingsView,
    WheelSettingsView,
)
from views.casino_games.slots_play import SlotsPlayView


def _is_adminish(interaction: discord.Interaction, settings: dict) -> bool:
    member = interaction.user
    role_ids = {r.id for r in getattr(member, "roles", [])}
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    admin_role = int(((settings.get("casino_house") or {}).get("casino_admin_role_id") or 0))
    if admin_role and admin_role in role_ids:
        return True
    allowed = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
    return bool(allowed.intersection(role_ids))


class CasinoCog(commands.Cog):
    jump = app_commands.Group(name="jump", description="Jump commands")

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @jump.command(name="casino", description="Open casino home")
    async def casino(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Guild only command.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=await casino_home_embed(interaction.guild_id, interaction.user.id),
            view=CasinoHomeView(interaction.guild_id),
            ephemeral=True,
        )

    @jump.command(name="house_settings", description="Open casino house settings")
    async def house_settings(self, interaction: discord.Interaction):
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        if not _is_adminish(interaction, settings):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return
        await interaction.response.send_message(embed=await house_settings_embed(interaction.guild_id), view=HouseSettingsView(interaction.guild_id), ephemeral=True)

    @jump.command(name="casino_deposit", description="Open casino deposit panel")
    async def casino_deposit(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await deposit_panel_embed(interaction.guild_id), view=DepositPanelView(interaction.guild_id), ephemeral=True)

    @jump.command(name="casino_cashout", description="Request casino cashout")
    async def casino_cashout(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CashoutRequestModal(interaction.guild_id))

    @jump.command(name="casino_ledger", description="Open casino ledger")
    async def casino_ledger(self, interaction: discord.Interaction):
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        if not _is_adminish(interaction, settings):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return
        view = AdminLedgerView(interaction.guild_id)
        await interaction.response.send_message("Casino ledger", view=view, ephemeral=True)
        await view.render(interaction)

    @jump.command(name="casino_slots_settings", description="Slots settings")
    async def casino_slots_settings(self, interaction: discord.Interaction):
        await interaction.response.send_message("Slots settings", view=SlotsSettingsView(interaction.guild_id), ephemeral=True)

    @jump.command(name="casino_roulette_settings", description="Roulette settings")
    async def casino_roulette_settings(self, interaction: discord.Interaction):
        await interaction.response.send_message("Roulette settings", view=RouletteSettingsView(interaction.guild_id), ephemeral=True)

    @jump.command(name="casino_wheel_settings", description="Wheel settings")
    async def casino_wheel_settings(self, interaction: discord.Interaction):
        await interaction.response.send_message("Wheel settings", view=WheelSettingsView(interaction.guild_id), ephemeral=True)

    @jump.command(name="casino_dice_settings", description="Dice settings")
    async def casino_dice_settings(self, interaction: discord.Interaction):
        await interaction.response.send_message("Dice settings", view=DiceSettingsView(interaction.guild_id), ephemeral=True)


    @app_commands.command(name="jumper_slots", description="Open Slots")
    @app_commands.guild_only()
    async def jumper_slots(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("Guild only command.", ephemeral=True)
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        if not settings.get("casino_enabled"):
            await interaction.response.send_message("Casino is disabled. Ask admins to run /jump house_settings and enable casino.", ephemeral=True)
            return
        service = CasinoSlotsService(get_pool())
        cfg = await service.ensure_slots_config(interaction.guild_id)
        if not cfg.get("enabled", True):
            await interaction.response.send_message("Slots is disabled in this server.", ephemeral=True)
            return
        try:
            snapshot = await service.get_balance_and_pool(interaction.guild_id, interaction.user.id)
        except SlotsError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        view = SlotsPlayView(
            guild_id=interaction.guild_id,
            discord_id=interaction.user.id,
            service=service,
            config=snapshot["config"],
            balance=int(snapshot["balance"]),
            pool_tokens=int(snapshot["pool_tokens"]),
            pool_millis=int(snapshot["pool_millis"]),
        )
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @jump.command(name="casino_play", description="Choose casino game")
    async def casino_play(self, interaction: discord.Interaction):
        labels = ", ".join(v.display_name for v in get_game_registry().values())
        await interaction.response.send_message(f"Games: {labels} (coming soon)", view=CasinoHomeView(interaction.guild_id), ephemeral=True)


async def setup(bot: commands.Bot):
    cog = CasinoCog(bot)
    await bot.add_cog(cog)
    try:
        bot.tree.add_command(cog.jump)
    except Exception:
        pass
    try:
        bot.tree.add_command(cog.jumper_slots)
    except Exception:
        pass
