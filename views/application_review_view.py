"""Persistent review action view for insurer and host99k applications."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import discord

from bot_actions.application_review import perform_application_review
from setup_panel import has_setup_permission
from utils import GuildSettingsRepository, get_database
from utils.embeds import create_info_embed

log = logging.getLogger("happy_jumper.application_review")


def _format_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "Unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


class DenyReasonModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(
        label="Reason",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, review_view: "ApplicationReviewView"):
        super().__init__()
        self.review_view = review_view

    async def on_submit(self, interaction: discord.Interaction):
        reason_text = self.reason.value.strip()
        if not reason_text:
            await interaction.response.send_message("Denial reason is required", ephemeral=True)
            return
        await self.review_view.handle_decision(interaction=interaction, decision="deny", reason=reason_text)


class ApproveButton(discord.ui.Button):
    def __init__(self, view_state: "ApplicationReviewView"):
        super().__init__(
            label="Approve",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=view_state.build_custom_id("approve"),
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, ApplicationReviewView):
            await interaction.response.send_message("View unavailable.", ephemeral=True)
            return
        await view.handle_decision(interaction=interaction, decision="approve")


class DenyButton(discord.ui.Button):
    def __init__(self, view_state: "ApplicationReviewView"):
        super().__init__(
            label="Deny",
            emoji="❌",
            style=discord.ButtonStyle.danger,
            custom_id=view_state.build_custom_id("deny"),
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, ApplicationReviewView):
            await interaction.response.send_message("View unavailable.", ephemeral=True)
            return
        await interaction.response.send_modal(DenyReasonModal(view))


class ApplicationReviewView(discord.ui.View):
    """Reusable persistent view for admin approve/deny actions."""

    def __init__(self, category: str, application_id: int, applicant_discord_id: int, guild_id: int):
        super().__init__(timeout=None)
        self.category = category
        self.application_id = int(application_id)
        self.applicant_discord_id = int(applicant_discord_id)
        self.guild_id = int(guild_id)

        self.add_item(ApproveButton(self))
        self.add_item(DenyButton(self))

    def build_custom_id(self, action: str) -> str:
        return f"app_review:{action}:{self.category}:{self.application_id}:{self.applicant_discord_id}:{self.guild_id}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return False

        db = get_database()
        repo = GuildSettingsRepository(db)
        settings = await repo.get_or_create(interaction.guild.id)
        admin_role_ids = GuildSettingsRepository.resolve_admin_role_ids(settings)

        allowed = has_setup_permission(
            member_id=interaction.user.id,
            guild_owner_id=interaction.guild.owner_id,
            is_administrator=interaction.user.guild_permissions.administrator,
            can_manage_guild=interaction.user.guild_permissions.manage_guild,
            member_role_ids={str(role.id) for role in interaction.user.roles},
            admin_role_ids=admin_role_ids,
        )
        if not allowed:
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return False
        return True

    async def _resolve_member(self, guild: discord.Guild, member_id: int) -> Optional[discord.Member]:
        member = guild.get_member(member_id)
        if member:
            return member
        try:
            return await guild.fetch_member(member_id)
        except Exception:
            return None

    async def _notify_applicant(self, interaction: discord.Interaction, decision: str, reason: Optional[str]) -> str:
        guild = interaction.guild
        if not guild:
            return "Could not resolve guild for DM."
        member = await self._resolve_member(guild, self.applicant_discord_id)
        if not member:
            return "Could not resolve applicant for DM."

        decision_word = "approved" if decision == "approve" else "denied"
        dm_embed = create_info_embed(
            "Application Review Result",
            f"Your **{self.category}** application (ID `{self.application_id}`) was **{decision_word}**."
            + (f"\nReason: {reason}" if reason and decision == "deny" else ""),
        )
        try:
            await member.send(embed=dm_embed)
            return "Applicant DM sent."
        except discord.Forbidden:
            return "Could not DM applicant (DMs disabled)."

    async def _edit_admin_message(self, interaction: discord.Interaction, result: dict, decision: str, reason: Optional[str]):
        if not interaction.message:
            return

        decision_word = "Approved" if decision == "approve" else "Denied"
        approver = interaction.user.mention
        approved_at = _format_dt(result.get("approved_at"))

        embed = discord.Embed(
            title=f"{self.category} application #{self.application_id} — {decision_word}",
            color=discord.Color.green() if decision == "approve" else discord.Color.red(),
        )
        embed.add_field(name="Applicant", value=f"<@{self.applicant_discord_id}> (`{self.applicant_discord_id}`)", inline=False)
        embed.add_field(name="Reviewed by", value=f"{approver}\n{approved_at}", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason[:1024], inline=False)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)

    async def handle_decision(self, interaction: discord.Interaction, decision: str, reason: Optional[str] = None):
        await interaction.response.defer(ephemeral=True)

        try:
            review = await perform_application_review(
                category=self.category,
                application_id=self.application_id,
                decision=decision,
                admin_discord_id=interaction.user.id,
                reason=reason,
                guild_id_hint=self.guild_id,
            )
        except RuntimeError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            log.exception("Failed to review application %s (%s)", self.application_id, self.category)
            await interaction.followup.send("Something went wrong while processing that decision. Please try again.", ephemeral=True)
            return

        if not review:
            await interaction.followup.send("Application not found.", ephemeral=True)
            return

        result = review["result"]
        dm_status = await self._notify_applicant(interaction, decision=decision, reason=reason)
        await self._edit_admin_message(interaction, result=result, decision=decision, reason=reason)

        verdict = "approved" if decision == "approve" else "denied"
        await interaction.followup.send(f"Application `{self.application_id}` {verdict}. {dm_status}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        log.exception("ApplicationReviewView error: %s", error)
        if interaction.response.is_done():
            await interaction.followup.send("An error occurred.", ephemeral=True)
        else:
            await interaction.response.send_message("An error occurred.", ephemeral=True)
