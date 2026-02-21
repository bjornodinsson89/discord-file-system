from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from services.casino_core.ledger import ledger_line
from utils.database import get_pool


class FilterModal(discord.ui.Modal, title="Ledger Filters"):
    user = discord.ui.TextInput(label="Wallet user ID", required=False, max_length=30)
    entry_type = discord.ui.TextInput(label="Entry type", required=False, max_length=40)

    def __init__(self, view: "AdminLedgerView"):
        super().__init__()
        self.v = view

    async def on_submit(self, interaction: discord.Interaction):
        self.v.wallet_filter = int(self.user.value) if str(self.user.value).strip().isdigit() else None
        self.v.entry_filter = str(self.entry_type.value).strip() or None
        await self.v.render(interaction)


class AdminLedgerView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.repo = CasinoCoreRepository(get_pool())
        self.page = 0
        self.mode = "token"
        self.wallet_filter = None
        self.entry_filter = None

    async def render(self, interaction: discord.Interaction):
        limit = 10
        offset = self.page * limit
        totals = await self.repo.compute_admin_totals(self.guild_id)
        if self.mode == "token":
            rows = await self.repo.fetch_ledger_page(self.guild_id, limit=limit, offset=offset, wallet_id=self.wallet_filter, entry_type=self.entry_filter)
            lines = [ledger_line(r) for r in rows] or ["No entries."]
            title = "Casino Token Ledger"
        else:
            rows = await self.repo.fetch_house_ledger_page(self.guild_id, limit=limit, offset=offset)
            lines = [f"`#{r['id']}` **{r['entry_type']}** {r['amount_tokens']} total={r['total_after']}" for r in rows] or ["No entries."]
            title = "Casino House Ledger"
        em = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.blurple())
        em.add_field(name="Totals", value=(
            f"Deposits: {totals.get('total_deposits_tokens', 0)}\n"
            f"Payouts: {totals.get('total_payouts_verified_tokens', 0)}\n"
            f"Escrow: {totals.get('escrow_outstanding_tokens', 0)}\n"
            f"Circulation: {totals.get('tokens_in_circulation', 0)}\n"
            f"House Net: {totals.get('house_net_tokens', 0)}"
        ), inline=False)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=em, view=self)
        else:
            await interaction.response.edit_message(embed=em, view=self)

    @discord.ui.button(label="Token Ledger", style=discord.ButtonStyle.primary)
    async def token(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.mode = "token"
        self.page = 0
        await self.render(interaction)

    @discord.ui.button(label="House Ledger", style=discord.ButtonStyle.primary)
    async def house(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.mode = "house"
        self.page = 0
        await self.render(interaction)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page = max(self.page - 1, 0)
        await self.render(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.page += 1
        await self.render(interaction)

    @discord.ui.button(label="Filters", style=discord.ButtonStyle.secondary)
    async def filters(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(FilterModal(self))
