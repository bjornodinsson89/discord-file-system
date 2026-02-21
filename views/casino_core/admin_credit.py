from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from utils.database import get_pool
from views.casino_core.permissions import ensure_casino_admin

AMOUNT_OPTIONS = ["-100", "-50", "-25", "-10", "-5", "-1", "+1", "+5", "+10", "+25", "+50", "+100"]
REASON_OPTIONS = ["Testing", "Payment verification failed", "Adjustment", "Refund", "Other"]


class _TargetSelect(discord.ui.UserSelect):
    def __init__(self, parent: "AdminCreditView"):
        super().__init__(placeholder="Select target user", min_values=1, max_values=1)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        self.parent_view.target_id = int(self.values[0].id)
        await interaction.response.edit_message(embed=self.parent_view.build_embed(), view=self.parent_view)


class _AmountSelect(discord.ui.Select):
    def __init__(self, parent: "AdminCreditView"):
        super().__init__(
            placeholder="Select amount",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=v, value=v) for v in AMOUNT_OPTIONS],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        self.parent_view.amount = int(self.values[0])
        await interaction.response.edit_message(embed=self.parent_view.build_embed(), view=self.parent_view)


class _ReasonSelect(discord.ui.Select):
    def __init__(self, parent: "AdminCreditView"):
        super().__init__(
            placeholder="Select reason",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=v, value=v) for v in REASON_OPTIONS],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.parent_view.guild_id):
            return
        self.parent_view.reason = self.values[0]
        await interaction.response.edit_message(embed=self.parent_view.build_embed(), view=self.parent_view)


class AdminCreditView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.repo = CasinoCoreRepository(get_pool())
        self.target_id: int | None = None
        self.amount: int | None = None
        self.reason: str = "Testing"
        self.add_item(_TargetSelect(self))
        self.add_item(_AmountSelect(self))
        self.add_item(_ReasonSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    def build_embed(self) -> discord.Embed:
        em = discord.Embed(title="Casino Admin Credit", color=discord.Color.green())
        em.add_field(name="Target", value=f"<@{self.target_id}>" if self.target_id else "Not selected")
        em.add_field(name="Amount", value=str(self.amount) if self.amount is not None else "Not selected")
        em.add_field(name="Reason", value=self.reason)
        return em

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success, row=3)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        if not self.target_id or self.amount is None:
            await interaction.response.send_message("❌ Select target user and amount first.", ephemeral=True)
            return

        wallet = await self.repo.get_or_create_wallet(
            guild_id=self.guild_id,
            discord_id=int(self.target_id),
            torn_user_id=0,
            torn_name=None,
        )

        async with self.repo.acquire() as conn:
            async with conn.transaction():
                wallet = await self.repo.apply_ledger_entry_atomic(
                    conn,
                    guild_id=int(self.guild_id),
                    wallet_id=int(wallet["id"]),
                    entry_type="admin_adjust",
                    amount_tokens=int(self.amount),
                    idempotency_key=f"admin_adjust:{self.guild_id}:{interaction.id}",
                    ref_type="admin_adjust",
                    ref_id=int(interaction.id),
                    metadata={
                        "by_discord_id": int(interaction.user.id),
                        "target_discord_id": int(self.target_id),
                        "reason": self.reason,
                    },
                )
                await self.repo.append_house_ledger(
                    conn,
                    guild_id=int(self.guild_id),
                    entry_type="admin_adjust",
                    amount_tokens=0,
                    ref_type="admin_adjust",
                    ref_id=int(interaction.id),
                    metadata={
                        "by_discord_id": int(interaction.user.id),
                        "target_discord_id": int(self.target_id),
                        "reason": self.reason,
                        "amount_tokens": int(self.amount),
                    },
                )

        await interaction.response.send_message(
            f"✅ Admin credit applied. Target: <@{int(self.target_id)}> | Amount: **{int(self.amount)}** | New balance: **{int(wallet.get('balance_tokens') or 0)}**",
            ephemeral=True,
        )
