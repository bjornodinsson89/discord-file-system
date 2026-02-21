from __future__ import annotations

import discord

from services.casino_core.cashouts import CasinoCashoutService
from views.casino_core.permissions import ensure_casino_admin


class CashoutRequestModal(discord.ui.Modal, title="Request Cashout"):
    amount = discord.ui.TextInput(label="Token amount", min_length=1, max_length=12)
    note = discord.ui.TextInput(label="Note", required=False, style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.service = CasinoCashoutService()

    async def on_submit(self, interaction: discord.Interaction):
        try:
            qty = int(str(self.amount.value).strip())
            cashout_id = await self.service.request_cashout(interaction, self.guild_id, int(interaction.user.id), qty, str(self.note.value).strip() or None)
            await interaction.response.send_message(f"✅ Cashout request submitted: #{cashout_id}", ephemeral=True)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Failed to submit cashout request.", ephemeral=True)


class DenyReasonModal(discord.ui.Modal, title="Deny Cashout"):
    reason = discord.ui.TextInput(label="Reason", required=True, style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, guild_id: int, cashout_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.cashout_id = cashout_id
        self.service = CasinoCashoutService()

    async def on_submit(self, interaction: discord.Interaction):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await self.service.deny_cashout(self.guild_id, self.cashout_id, int(interaction.user.id), str(self.reason.value))
        await interaction.response.send_message("✅ Cashout denied and refunded.", ephemeral=True)


class HouseCashoutActionView(discord.ui.View):
    def __init__(self, guild_id: int, cashout_id: int):
        super().__init__(timeout=86400)
        self.guild_id = guild_id
        self.cashout_id = cashout_id
        self.service = CasinoCashoutService()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await ensure_casino_admin(interaction, self.guild_id)

    def _disable(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Verify payout sent", style=discord.ButtonStyle.success)
    async def verify(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        try:
            ok = await self.service.verify_payout(interaction, self.guild_id, self.cashout_id)
        except ValueError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return
        except Exception:
            await interaction.response.send_message("❌ Failed to verify payout.", ephemeral=True)
            return
        if ok:
            self._disable()
            await interaction.response.edit_message(content="✅ Payout verified.", view=self)
        else:
            await interaction.response.send_message("⚠️ Matching payout not found yet.", ephemeral=True)

    @discord.ui.button(label="Deny & refund", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await ensure_casino_admin(interaction, self.guild_id):
            return
        await interaction.response.send_modal(DenyReasonModal(self.guild_id, self.cashout_id))
