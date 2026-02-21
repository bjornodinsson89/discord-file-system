from __future__ import annotations

import discord

from repositories.casino_core import CasinoCoreRepository
from views.casino_core.permissions import ensure_casino_admin
from views.casino_core.shared import parse_snowflake
from utils.database import get_pool


class AdminCreditModal(discord.ui.Modal, title="Casino Admin Credit"):
    target_user = discord.ui.TextInput(label="Target user (mention or ID)", required=True, max_length=64)
    amount = discord.ui.TextInput(label="Amount (integer; negative deducts)", required=True, max_length=20)
    reason = discord.ui.TextInput(label="Reason", required=False, style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.repo = CasinoCoreRepository(get_pool())

    async def on_submit(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return

        target_id = parse_snowflake(str(self.target_user.value))
        if not target_id:
            await interaction.response.send_message("❌ Invalid target user.", ephemeral=True)
            return
        try:
            amount = int(str(self.amount.value).strip())
        except Exception:
            await interaction.response.send_message("❌ Amount must be an integer.", ephemeral=True)
            return

        reason = str(self.reason.value).strip()
        wallet = await self.repo.get_or_create_wallet(
            guild_id=self.guild_id,
            discord_id=int(target_id),
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
                    amount_tokens=int(amount),
                    idempotency_key=f"admin_adjust:{self.guild_id}:{interaction.id}",
                    ref_type="admin_adjust",
                    ref_id=int(interaction.id),
                    metadata={
                        "by_discord_id": int(interaction.user.id),
                        "target_discord_id": int(target_id),
                        "reason": reason,
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
                        "target_discord_id": int(target_id),
                        "reason": reason,
                        "amount_tokens": int(amount),
                    },
                )

        await interaction.response.send_message(
            f"✅ Admin credit applied. Target: <@{int(target_id)}> | Amount: **{int(amount)}** | New balance: **{int(wallet.get('balance_tokens') or 0)}**",
            ephemeral=True,
        )
