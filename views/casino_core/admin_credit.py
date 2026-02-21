from __future__ import annotations

import discord

from views.casino_core.permissions import ensure_casino_admin

AMOUNT_OPTIONS = [-100, -50, -25, -10, -5, -1, 1, 5, 10, 25, 50, 100]
REASON_OPTIONS = ["Testing", "Payment verification failed", "Adjustment", "Refund", "Other"]


class _TargetSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Select target user", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, AdminCreditView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.target_user_id = int(self.values[0].id)
        await interaction.response.defer()


class _AmountSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f"{amount:+d}", value=str(amount))
            for amount in AMOUNT_OPTIONS
        ]
        super().__init__(placeholder="Select amount", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, AdminCreditView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.amount = int(self.values[0])
        await interaction.response.defer()


class _ReasonSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=reason, value=reason) for reason in REASON_OPTIONS]
        super().__init__(placeholder="Select reason", min_values=0, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, AdminCreditView):
            return
        if not await ensure_casino_admin(interaction, view.guild_id):
            return
        view.reason = self.values[0] if self.values else ""
        await interaction.response.defer()


class AdminCreditView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.repo = None
        self.target_user_id: int | None = None
        self.amount: int | None = None
        self.reason: str = ""
        self.add_item(_TargetSelect())
        self.add_item(_AmountSelect())
        self.add_item(_ReasonSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply_credit(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        if not self.target_user_id:
            await interaction.response.send_message("❌ Pick a target user first.", ephemeral=True)
            return
        if self.amount is None:
            await interaction.response.send_message("❌ Pick an amount first.", ephemeral=True)
            return

        from repositories.casino_core import CasinoCoreRepository
        from utils.database import get_pool

        repo = self.repo or CasinoCoreRepository(get_pool())
        self.repo = repo

        wallet = await repo.get_or_create_wallet(
            guild_id=self.guild_id,
            discord_id=int(self.target_user_id),
            torn_user_id=0,
            torn_name=None,
        )

        async with repo.acquire() as conn:
            async with conn.transaction():
                wallet = await repo.apply_ledger_entry_atomic(
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
                        "target_discord_id": int(self.target_user_id),
                        "reason": self.reason,
                    },
                )
                await repo.append_house_ledger(
                    conn,
                    guild_id=int(self.guild_id),
                    entry_type="admin_adjust",
                    amount_tokens=0,
                    ref_type="admin_adjust",
                    ref_id=int(interaction.id),
                    metadata={
                        "by_discord_id": int(interaction.user.id),
                        "target_discord_id": int(self.target_user_id),
                        "reason": self.reason,
                        "amount_tokens": int(self.amount),
                    },
                )

        await interaction.response.send_message(
            f"✅ Admin credit applied. Target: <@{int(self.target_user_id)}> | Amount: **{int(self.amount):+d}** | New balance: **{int(wallet.get('balance_tokens') or 0)}**",
            ephemeral=True,
        )
