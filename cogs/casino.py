from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.casino_core.registry import get_game_registry
from services.casino_games.slots import CasinoSlotsService, SlotsError
from utils import GuildSettingsRepository, get_database
from utils.database import get_pool
from views.casino_core import (
    CasinoHomeView,
    CashoutRequestModal,
    DepositPanelView,
    casino_home_embed,
    deposit_panel_embed,
)
from views.casino_core.back_of_house import BackOfHouseView, back_of_house_embed
from views.casino_core.ledger_panel import send_admin_ledger_panel
from views.casino_core.permissions import ensure_casino_admin
from views.casino_core.shared import ComingSoonView


async def open_slots_panel(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("Guild only command.", ephemeral=True)
        return
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not settings.get("casino_enabled"):
        await interaction.response.send_message(
            "Casino is disabled. Ask admins to run /back_of_house and enable casino.",
            ephemeral=True,
        )
        return
    try:
        from views.casino_games.slots_play import SlotsPlayView
    except Exception:
        await interaction.response.send_message(
            "Slots module not installed yet.",
            ephemeral=True,
            view=ComingSoonView("Slots module not installed yet."),
        )
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

    @jump.command(name="casino_deposit", description="Open casino deposit panel")
    async def casino_deposit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=await deposit_panel_embed(interaction.guild_id),
            view=DepositPanelView(interaction.guild_id),
            ephemeral=True,
        )

    @jump.command(name="casino_cashout", description="Request casino cashout")
    async def casino_cashout(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CashoutRequestModal(interaction.guild_id))

    @jump.command(name="casino_ledger", description="Open casino ledger")
    async def casino_ledger(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await send_admin_ledger_panel(interaction, interaction.guild_id)

    @jump.command(name="casino_admin_credit", description="Open casino admin credit")
    async def casino_admin_credit(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        from views.casino_core.admin_credit import AdminCreditModal

        await interaction.response.send_modal(AdminCreditModal(interaction.guild_id))

    @jump.command(name="house_settings", description="Deprecated: use /back_of_house")
    async def house_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @jump.command(name="casino_game_settings", description="Deprecated: use /back_of_house")
    async def casino_game_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @jump.command(name="casino_slots_settings", description="Deprecated: use /back_of_house")
    async def casino_slots_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @jump.command(name="casino_roulette_settings", description="Deprecated: use /back_of_house")
    async def casino_roulette_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @jump.command(name="casino_wheel_settings", description="Deprecated: use /back_of_house")
    async def casino_wheel_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @jump.command(name="casino_dice_settings", description="Deprecated: use /back_of_house")
    async def casino_dice_settings(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message("Use /back_of_house", ephemeral=True)

    @app_commands.command(name="back_of_house", description="Casino back of house")
    @app_commands.guild_only()
    async def back_of_house(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, interaction.guild_id):
            return
        await interaction.response.send_message(
            embed=await back_of_house_embed(interaction.guild_id),
            view=BackOfHouseView(interaction.guild_id),
            ephemeral=True,
        )

    @app_commands.command(name="jumper_slots", description="Open Slots")
    @app_commands.guild_only()
    async def jumper_slots(self, interaction: discord.Interaction):
        await open_slots_panel(interaction)

    @jump.command(name="casino_play", description="Choose casino game")
    async def casino_play(self, interaction: discord.Interaction):
        labels = ", ".join(v.display_name for v in get_game_registry().values())
        await interaction.response.send_message(
            f"Games: {labels}.",
            view=CasinoHomeView(interaction.guild_id),
            ephemeral=True,
        )


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
    try:
        bot.tree.add_command(cog.back_of_house)
    except Exception:
        pass
