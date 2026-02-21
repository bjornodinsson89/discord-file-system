from __future__ import annotations

import discord

from services.casino_core.deposits import CasinoDepositService
from services.casino_core.settings import get_house_config
from utils import GuildSettingsRepository, get_database


class DepositPanelView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.service = CasinoDepositService()

    @discord.ui.button(label="Verify Deposit", style=discord.ButtonStyle.success)
    async def verify_deposit(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            summary = await self.service.verify_and_credit(interaction, self.guild_id, int(interaction.user.id))
            await interaction.response.send_message(
                f"✅ Credited {summary['credited_total']} token(s) across {summary['count']} deposit(s). Balance: {summary['new_balance']}",
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Failed to verify deposit.", ephemeral=True)


async def deposit_panel_embed(guild_id: int) -> discord.Embed:
    settings = await GuildSettingsRepository(get_database()).get_or_create(guild_id)
    house = get_house_config(settings)
    em = discord.Embed(title="Casino Deposit", color=discord.Color.green())
    em.description = (
        f"Send Xanax (`item_id=206`) to house Torn ID **{house.get('house_torn_id') or 'Not set'}** then press **Verify Deposit**.\n"
        "Conversion: **1 Xanax = 1 token**"
    )
    return em
