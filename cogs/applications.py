from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from repositories.host_apps import HostAppsRepository
from repositories.insurance_apps import InsuranceAppsRepository
from utils import GuildSettingsRepository, get_database

logger = logging.getLogger(__name__)

HOST_QUESTIONS = [
    "What is your timezone? (example: MST)",
    "What hours are you usually available to host?",
    "How many 99k jumps have you hosted before (if any)?",
    "Confirm you understand you must follow the jump rules and keep the session organized. (Type: I AGREE)",
    "Anything else you want admins to know?",
]

INSURER_QUESTIONS = [
    "What is your timezone? (example: MST)",
    "Describe your insurance terms (coverage, limits, claim rules).",
    "What is your preferred contact method (Discord DM, channel, etc.)?",
    "Do you have references or prior insurer experience? (Explain briefly)",
    "Anything else you want admins to know?",
]


def _sanitize(text: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", text.lower())
    return re.sub(r"-+", "-", value).strip("-")[:70] or "applicant"


def _is_admin(member: discord.Member, settings: dict[str, Any]) -> bool:
    if member.guild_permissions.administrator:
        return True
    allowed = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
    return bool(allowed.intersection({r.id for r in member.roles}))


class AnswerModal(discord.ui.Modal):
    answer = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, max_length=1000, required=True)

    def __init__(self, cog: "ApplicationsCog", app_type: str, app_id: int, current_question: int):
        super().__init__(title=f"App #{app_id} — Q{current_question}/5")
        self.cog = cog
        self.app_type = app_type
        self.app_id = app_id
        self.current_question = current_question

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_answer(interaction, self.app_type, self.app_id, self.current_question, str(self.answer.value).strip())


class DenyModal(discord.ui.Modal, title="Deny Application"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=1000, required=True)

    def __init__(self, cog: "ApplicationsCog", app_type: str, app_id: int):
        super().__init__()
        self.cog = cog
        self.app_type = app_type
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.review_application(interaction, self.app_type, self.app_id, approve=False, reason=str(self.reason.value).strip())


class HostAppAnswerView(discord.ui.View):
    def __init__(self, app_id: int | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Answer", style=discord.ButtonStyle.primary, custom_id=f"ha:ans:{app_id or 0}"))


class InsuranceAppAnswerView(discord.ui.View):
    def __init__(self, app_id: int | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Answer", style=discord.ButtonStyle.primary, custom_id=f"ia:ans:{app_id or 0}"))


class AdminInboxHostAppView(discord.ui.View):
    def __init__(self, app_id: int | None = None, jump_url: str | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Open", style=discord.ButtonStyle.link, url=jump_url or "https://discord.com"))
        self.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id=f"ha:cl:{app_id or 0}"))
        self.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id=f"ha:del:{app_id or 0}"))


class AdminInboxInsuranceAppView(discord.ui.View):
    def __init__(self, app_id: int | None = None, jump_url: str | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Open", style=discord.ButtonStyle.link, url=jump_url or "https://discord.com"))
        self.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id=f"ia:cl:{app_id or 0}"))
        self.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id=f"ia:del:{app_id or 0}"))


class HostAppReviewView(discord.ui.View):
    def __init__(self, app_id: int | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"ha:ok:{app_id or 0}"))
        self.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"ha:no:{app_id or 0}"))


class InsuranceAppReviewView(discord.ui.View):
    def __init__(self, app_id: int | None = None):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"ia:ok:{app_id or 0}"))
        self.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"ia:no:{app_id or 0}"))


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(HostAppAnswerView())
        self.bot.add_view(InsuranceAppAnswerView())
        self.bot.add_view(AdminInboxHostAppView())
        self.bot.add_view(AdminInboxInsuranceAppView())
        self.bot.add_view(HostAppReviewView())
        self.bot.add_view(InsuranceAppReviewView())

    async def cog_load(self) -> None:
        self.bot.add_listener(self.on_interaction, "on_interaction")

    async def _repos(self):
        db = get_database()
        return HostAppsRepository(db.pool), InsuranceAppsRepository(db.pool), GuildSettingsRepository(db)

    def _coerce_answers(self, raw: Any) -> dict[str, str]:
        if raw is None:
            return {}
        if isinstance(raw, dict):
            return {str(key): "" if value is None else str(value) for key, value in raw.items()}
        if isinstance(raw, str):
            stripped = raw.strip()
            if not stripped:
                return {}
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    return {str(key): "" if value is None else str(value) for key, value in obj.items()}
            except Exception:
                return {}
        return {}

    def _summary_embed(self, app: dict[str, Any], member: discord.Member | None, app_type: str) -> discord.Embed:
        answers = self._coerce_answers(app.get("answers"))
        embed = discord.Embed(title=f"{'Host' if app_type == 'host' else 'Insurance'} Application #{app['id']}")
        embed.add_field(name="Applicant", value=f"{member.mention if member else app['applicant_discord_id']} (`{app['applicant_discord_id']}`)", inline=False)
        embed.add_field(name="Status", value=str(app.get("status") or "in_progress"), inline=False)
        questions = HOST_QUESTIONS if app_type == "host" else INSURER_QUESTIONS
        for idx, _ in enumerate(questions, start=1):
            embed.add_field(name=f"Q{idx}", value=answers.get(f"q{idx}") or "— Pending —", inline=False)
        return embed

    async def _post_or_update_summary(self, channel: discord.TextChannel, app: dict[str, Any], app_type: str, repo: Any, member: discord.Member | None) -> None:
        embed = self._summary_embed(app, member, app_type)
        message = None
        if app.get("summary_message_id"):
            try:
                message = await channel.fetch_message(int(app["summary_message_id"]))
                await message.edit(embed=embed)
            except Exception:
                message = None
        if message is None:
            message = await channel.send(embed=embed)
            await message.pin()
            await repo.set_summary_message_id(int(app["id"]), int(message.id))

    async def _start(self, interaction: discord.Interaction, app_type: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        host_repo, insurance_repo, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(interaction.guild.id)
        repo = host_repo if app_type == "host" else insurance_repo
        open_app = await repo.get_open_app(interaction.guild.id, interaction.user.id)
        if open_app:
            await interaction.response.send_message(
                f"You already have an open application: #{open_app['id']} — <#{open_app['application_channel_id']}>",
                ephemeral=True,
            )
            return

        category = interaction.guild.get_channel(int(settings.get("applications_category_id") or 0))
        if not isinstance(category, discord.CategoryChannel):
            category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None

        bot_member = interaction.guild.me
        if not bot_member:
            await interaction.response.send_message("Bot not ready.", ephemeral=True)
            return

        overwrites: dict[Any, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
        }
        for role_id in GuildSettingsRepository.resolve_admin_role_ids(settings):
            role = interaction.guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        prefix = "host-app" if app_type == "host" else "insurer-app"
        app_channel = await interaction.guild.create_text_channel(
            name=f"{prefix}-{_sanitize(interaction.user.display_name)}",
            category=category,
            overwrites=overwrites,
            reason="Application channel",
        )
        app = await repo.create_app(interaction.guild.id, interaction.user.id, app_channel.id)
        await app_channel.send(f"Application #{app['id']} — {'Host' if app_type == 'host' else 'Insurance'} for {interaction.user.mention}")
        await self._post_or_update_summary(app_channel, app, app_type, repo, interaction.user)
        q = HOST_QUESTIONS[0] if app_type == "host" else INSURER_QUESTIONS[0]
        view = HostAppAnswerView(int(app["id"])) if app_type == "host" else InsuranceAppAnswerView(int(app["id"]))
        await app_channel.send(f"Application #{app['id']} — Q1/5: {q}", view=view)

        inbox_key = "host_apps_admin_inbox_channel_id" if app_type == "host" else "insurance_apps_admin_inbox_channel_id"
        inbox = interaction.guild.get_channel(int(settings.get(inbox_key) or 0))
        if isinstance(inbox, discord.TextChannel):
            inbox_view = AdminInboxHostAppView(int(app["id"]), app_channel.jump_url) if app_type == "host" else AdminInboxInsuranceAppView(int(app["id"]), app_channel.jump_url)
            msg = await inbox.send(f"**New {'Host' if app_type == 'host' else 'Insurance'} Application #{app['id']}** — {interaction.user.mention} — {app_channel.mention}", view=inbox_view)
            await repo.set_admin_inbox_message_id(int(app["id"]), int(msg.id))
        await interaction.response.send_message(f"Application started: {app_channel.mention}", ephemeral=True)

    @app_commands.command(name="apply_99k_host", description="Apply as a 99k host")
    async def apply_99k_host(self, interaction: discord.Interaction):
        await self._start(interaction, "host")

    @app_commands.command(name="apply_insurance_provider", description="Apply as insurance provider")
    async def apply_insurance_provider(self, interaction: discord.Interaction):
        await self._start(interaction, "insurance")

    async def submit_answer(self, interaction: discord.Interaction, app_type: str, app_id: int, current_question: int, answer: str) -> None:
        deferred = False
        try:
            await interaction.response.defer(thinking=False)
            deferred = True
            host_repo, insurance_repo, _, = await self._repos()
            repo = host_repo if app_type == "host" else insurance_repo
            app = await repo.get_by_id(app_id)
            if not app or app.get("status") != "in_progress" or int(app.get("applicant_discord_id") or 0) != interaction.user.id:
                return
            updated = await repo.advance_answer(app_id, current_question, answer)
            if not updated:
                return
            channel = interaction.guild.get_channel(int(updated["application_channel_id"])) if interaction.guild else None
            if not isinstance(channel, discord.TextChannel):
                return
            member = interaction.guild.get_member(int(updated["applicant_discord_id"])) if interaction.guild else None
            await self._post_or_update_summary(channel, updated, app_type, repo, member)
            if updated.get("status") == "submitted":
                view = HostAppReviewView(app_id) if app_type == "host" else InsuranceAppReviewView(app_id)
                await channel.send("✅ Submitted for review.", view=view)
            else:
                nxt = int(updated.get("current_question") or 1)
                q = (HOST_QUESTIONS if app_type == "host" else INSURER_QUESTIONS)[nxt - 1]
                view = HostAppAnswerView(app_id) if app_type == "host" else InsuranceAppAnswerView(app_id)
                await channel.send(f"Application #{app_id} — Q{nxt}/5: {q}", view=view)
        except Exception:
            logger.exception("Failed submitting application answer")
            if not deferred and not interaction.response.is_done():
                await interaction.response.send_message("Failed to save answer.", ephemeral=True)
            else:
                await interaction.followup.send("Failed to save answer.", ephemeral=True)

    async def _update_admin_inbox(self, guild: discord.Guild, app: dict[str, Any], app_type: str, state: str) -> None:
        inbox_key = "host_apps_admin_inbox_channel_id" if app_type == "host" else "insurance_apps_admin_inbox_channel_id"
        _, _, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(guild.id)
        inbox = guild.get_channel(int(settings.get(inbox_key) or 0))
        if not isinstance(inbox, discord.TextChannel):
            return
        msg_id = int(app.get("admin_inbox_message_id") or 0)
        if not msg_id:
            return
        try:
            msg = await inbox.fetch_message(msg_id)
            await msg.edit(content=f"{msg.content} — {state}")
        except Exception:
            return

    async def _finalize_application(
        self,
        *,
        guild: discord.Guild,
        app_type: Literal["host", "insurance"],
        app: dict[str, Any],
        outcome: Literal["approved", "denied"],
        denial_reason: str | None,
        role_id_to_grant: int | None,
        repo: Any,
    ) -> None:
        applicant_id = int(app["applicant_discord_id"])
        member = guild.get_member(applicant_id)
        if not member:
            try:
                member = await guild.fetch_member(applicant_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None

        role_name = ""
        if outcome == "approved":
            if role_id_to_grant is None:
                raise RuntimeError("No role is configured for approved applications.")
            if member is None:
                raise RuntimeError("Applicant is no longer in the server, so the role cannot be granted.")
            role = guild.get_role(role_id_to_grant)
            me = guild.me
            if not role or not me or not me.top_role or role >= me.top_role:
                raise RuntimeError("I can't grant the configured role. Check role configuration and hierarchy.")
            try:
                await member.add_roles(role, reason=f"{app_type} application #{app['id']} approved")
                role_name = role.name
            except discord.Forbidden as exc:
                raise RuntimeError("I can't grant the configured role. Check my role permissions and hierarchy.") from exc

        app_label = "Host" if app_type == "host" else "Insurance"
        if outcome == "approved":
            dm_text = (
                f"✅ Your {app_label} application (#{app['id']}) was approved. "
                f"You’ve been granted the {role_name} role."
            )
        else:
            dm_text = f"❌ Your {app_label} application (#{app['id']}) was denied.\nReason: {denial_reason or 'No reason provided.'}"
        try:
            if member is not None:
                await member.send(dm_text)
        except discord.Forbidden:
            pass

        channel_id = int(app.get("application_channel_id") or 0)
        channel = guild.get_channel(channel_id)
        if channel is None and channel_id:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                channel = None
            except discord.Forbidden:
                channel = None
        if channel:
            try:
                await channel.delete(reason=f"{app_type} application #{app['id']} {outcome}")
            except discord.Forbidden:
                pass
            except discord.NotFound:
                pass

        await repo.delete_app(int(app["id"]))

    async def review_application(self, interaction: discord.Interaction, app_type: str, app_id: int, approve: bool, reason: str | None = None) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        host_repo, insurance_repo, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(interaction.guild.id)
        if not _is_admin(interaction.user, settings):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        repo = host_repo if app_type == "host" else insurance_repo
        current = await repo.get_by_id(app_id)
        if not current or current.get("status") != "submitted":
            await interaction.response.send_message("Application must be submitted first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        role_id_to_grant: int | None = None
        if approve:
            role_key = "host99k_role_id" if app_type == "host" else "insurer_role_id"
            role_id_to_grant = int(settings.get(role_key) or 0) or None
        try:
            await self._finalize_application(
                guild=interaction.guild,
                app_type="host" if app_type == "host" else "insurance",
                app=current,
                outcome="approved" if approve else "denied",
                denial_reason=reason,
                role_id_to_grant=role_id_to_grant,
                repo=repo,
            )
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if interaction.message:
            try:
                await interaction.message.edit(view=None)
            except Exception:
                pass

    async def _require_admin(self, interaction: discord.Interaction) -> tuple[bool, dict[str, Any] | None]:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return False, None
        _, _, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(interaction.guild.id)
        if not _is_admin(interaction.user, settings):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return False, None
        return True, settings

    async def _get_app_for_channel(self, guild_id: int, channel_id: int) -> tuple[str, dict[str, Any]] | tuple[None, None]:
        host_repo, insurance_repo, _ = await self._repos()
        host_app = await host_repo.get_by_channel_id(guild_id, channel_id)
        if host_app:
            return "host", host_app
        insurance_app = await insurance_repo.get_by_channel_id(guild_id, channel_id)
        if insurance_app:
            return "insurance", insurance_app
        return None, None

    async def _close_app(self, interaction: discord.Interaction, app_type: str, app: dict[str, Any]) -> bool:
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        updated = await repo.close_app(int(app["id"]), interaction.user.id)
        if not updated:
            return False
        if interaction.guild:
            await self._update_admin_inbox(interaction.guild, updated, app_type, "CLOSED")
        channel = interaction.guild.get_channel(int(updated.get("application_channel_id") or 0)) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"Closed by {interaction.user.mention}")
        return True

    async def _delete_app(self, interaction: discord.Interaction, app_type: str, app: dict[str, Any]) -> tuple[bool, str | None]:
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        channel = interaction.guild.get_channel(int(app.get("application_channel_id") or 0)) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.delete(reason=f"{app_type} app deleted by {interaction.user}")
            except discord.Forbidden:
                return False, "I don't have permission to delete this channel."
            except discord.HTTPException:
                return False, "Failed to delete the channel."
        elif interaction.channel_id == int(app.get("application_channel_id") or 0):
            return False, "This channel no longer exists."
        deleted = await repo.delete_app(int(app["id"]))
        if deleted and interaction.guild:
            await self._update_admin_inbox(interaction.guild, app, app_type, "DELETED")
        return deleted, None

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        parts = custom_id.split(":")
        if len(parts) != 3:
            return
        kind, action, raw_id = parts
        if kind not in {"ha", "ia"}:
            return
        app_type = "host" if kind == "ha" else "insurance"
        try:
            app_id = int(raw_id)
        except ValueError:
            return
        if action == "ans":
            host_repo, insurance_repo, _ = await self._repos()
            repo = host_repo if app_type == "host" else insurance_repo
            app = await repo.get_by_id(app_id)
            if not app or int(app.get("applicant_discord_id") or 0) != interaction.user.id:
                await interaction.response.send_message("Only the applicant can answer.", ephemeral=True)
                return
            q = int(app.get("current_question") or 1)
            await interaction.response.send_modal(AnswerModal(self, app_type, app_id, q))
            return
        if action == "ok":
            await self.review_application(interaction, app_type, app_id, approve=True)
            return
        if action == "no":
            await interaction.response.send_modal(DenyModal(self, app_type, app_id))
            return
        if action in {"cl", "del"}:
            ok, _ = await self._require_admin(interaction)
            if not ok:
                return
            await interaction.response.defer(ephemeral=True)
            host_repo, insurance_repo, _ = await self._repos()
            repo = host_repo if app_type == "host" else insurance_repo
            app = await repo.get_by_id(app_id)
            if not app:
                await interaction.followup.send("Application not found.", ephemeral=True)
                return
            if action == "cl":
                closed = await self._close_app(interaction, app_type, app)
                if not closed:
                    await interaction.followup.send("Application not found.", ephemeral=True)
                    return
                if interaction.message:
                    await interaction.message.edit(content=f"{interaction.message.content} — CLOSED")
                return
            deleted, error = await self._delete_app(interaction, app_type, app)
            if error:
                await interaction.followup.send(error, ephemeral=True)
                return
            if not deleted:
                await interaction.followup.send("Application not found.", ephemeral=True)
                return
            if interaction.message:
                await interaction.message.edit(content=f"{interaction.message.content} — DELETED")

    @app_commands.command(name="app_close", description="Close the application linked to this channel")
    async def app_close(self, interaction: discord.Interaction):
        ok, _ = await self._require_admin(interaction)
        if not ok:
            return
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        app_type, app = await self._get_app_for_channel(interaction.guild.id, interaction.channel_id)
        if not app_type or not app:
            await interaction.response.send_message("This channel is not an application channel.", ephemeral=True)
            return
        closed = await self._close_app(interaction, app_type, app)
        if not closed:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        await interaction.response.send_message("Closed.", ephemeral=True)

    @app_commands.command(name="app_delete", description="Delete the application linked to this channel")
    async def app_delete(self, interaction: discord.Interaction):
        ok, _ = await self._require_admin(interaction)
        if not ok:
            return
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        app_type, app = await self._get_app_for_channel(interaction.guild.id, interaction.channel_id)
        if not app_type or not app:
            await interaction.response.send_message("This channel is not an application channel.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        deleted, error = await self._delete_app(interaction, app_type, app)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        if not deleted:
            await interaction.followup.send("Application not found.", ephemeral=True)

    @app_commands.command(name="app_list_open", description="List open applications")
    @app_commands.describe(type="Application type", user="Filter by applicant")
    @app_commands.choices(type=[
        app_commands.Choice(name="host", value="host"),
        app_commands.Choice(name="insurance", value="insurance"),
    ])
    async def app_list_open(self, interaction: discord.Interaction, type: app_commands.Choice[str], user: discord.Member | None = None):
        ok, _ = await self._require_admin(interaction)
        if not ok:
            return
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if type.value == "host" else insurance_repo
        open_apps = await repo.list_open(interaction.guild.id, user.id if user else None)
        if not open_apps:
            await interaction.response.send_message("No open applications found.", ephemeral=True)
            return
        lines = []
        for app in open_apps[:15]:
            applicant_id = int(app.get("applicant_discord_id") or 0)
            channel_id = int(app.get("application_channel_id") or 0)
            lines.append(f"#{app['id']} — <@{applicant_id}> — <#{channel_id}> — {app['status']}")
        if len(open_apps) > 15:
            lines.append(f"and {len(open_apps) - 15} more…")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
