from __future__ import annotations

import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from repositories.host_apps import HostAppsRepository
from repositories.insurance_apps import InsuranceAppsRepository
from utils import GuildSettingsRepository, get_database

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

    def _summary_embed(self, app: dict[str, Any], member: discord.Member | None, app_type: str) -> discord.Embed:
        answers = app.get("answers") or {}
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
        host_repo, insurance_repo, _, = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        app = await repo.get_by_id(app_id)
        if not app or app.get("status") != "in_progress" or int(app.get("applicant_discord_id") or 0) != interaction.user.id:
            await interaction.response.send_message("You cannot answer this application.", ephemeral=True)
            return
        updated = await repo.advance_answer(app_id, current_question, answer)
        if not updated:
            await interaction.response.send_message("Question is out of sync.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(updated["application_channel_id"])) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Channel missing.", ephemeral=True)
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
        await interaction.response.send_message("Answer saved.", ephemeral=True)


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
        status = "approved" if approve else "denied"
        current = await repo.get_by_id(app_id)
        if not current or current.get("status") != "submitted":
            await interaction.response.send_message("Application must be submitted first.", ephemeral=True)
            return
        app = await repo.set_status(app_id, status, interaction.user.id, reason)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        if approve:
            role_key = "host99k_role_id" if app_type == "host" else "insurer_role_id"
            role = interaction.guild.get_role(int(settings.get(role_key) or 0))
            member = interaction.guild.get_member(int(app["applicant_discord_id"]))
            if role and member:
                await member.add_roles(role, reason=f"{app_type} application approved")
            await interaction.response.send_message(f"✅ Approved by {interaction.user.mention}", ephemeral=True)
            await self._update_admin_inbox(interaction.guild, app, app_type, "APPROVED")
        else:
            await interaction.response.send_message(f"❌ Denied by {interaction.user.mention} — Reason: {reason}", ephemeral=True)
            await self._update_admin_inbox(interaction.guild, app, app_type, "DENIED")

    async def _close_or_delete(self, interaction: discord.Interaction, app_type: str, app_id: int, delete: bool) -> str:
        host_repo, insurance_repo, settings_repo = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        app = await repo.get_by_id(app_id)
        if not app:
            return "Application not found."
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return "Server only."
        settings = await settings_repo.get_or_create(interaction.guild.id)
        if not _is_admin(interaction.user, settings):
            return "Admin only."
        if delete:
            channel = interaction.guild.get_channel(int(app.get("application_channel_id") or 0))
            if isinstance(channel, discord.TextChannel):
                await channel.delete(reason=f"{app_type} app deleted")
            await repo.delete_app(app_id)
            await self._update_admin_inbox(interaction.guild, app, app_type, "DELETED")
            return f"Deleted #{app_id}."
        updated = await repo.close_app(app_id, interaction.user.id)
        if updated:
            await self._update_admin_inbox(interaction.guild, updated, app_type, "CLOSED")
        channel = interaction.guild.get_channel(int(app.get("application_channel_id") or 0))
        if isinstance(channel, discord.TextChannel):
            await channel.send(f"Closed by {interaction.user.mention}")
        return f"Closed #{app_id}."

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
            msg = await self._close_or_delete(interaction, app_type, app_id, delete=action == "del")
            await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="host_app_close", description="Close host app")
    async def host_app_close(self, interaction: discord.Interaction, application_id: int, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        await interaction.response.send_message(await self._close_or_delete(interaction, "host", application_id, delete=False), ephemeral=True)

    @app_commands.command(name="host_app_delete", description="Delete host app")
    async def host_app_delete(self, interaction: discord.Interaction, application_id: int, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        await interaction.response.send_message(await self._close_or_delete(interaction, "host", application_id, delete=True), ephemeral=True)

    @app_commands.command(name="insurance_app_close", description="Close insurance app")
    async def insurance_app_close(self, interaction: discord.Interaction, application_id: int, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        await interaction.response.send_message(await self._close_or_delete(interaction, "insurance", application_id, delete=False), ephemeral=True)

    @app_commands.command(name="insurance_app_delete", description="Delete insurance app")
    async def insurance_app_delete(self, interaction: discord.Interaction, application_id: int, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        await interaction.response.send_message(await self._close_or_delete(interaction, "insurance", application_id, delete=True), ephemeral=True)

    @app_commands.command(name="host_app_delete_user", description="Delete open host app by user")
    async def host_app_delete_user(self, interaction: discord.Interaction, user: discord.Member, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        host_repo, _, _ = await self._repos()
        app = await host_repo.get_open_app(interaction.guild.id, user.id) if interaction.guild else None
        if not app:
            await interaction.response.send_message("No open host app for user.", ephemeral=True)
            return
        result = await self._close_or_delete(interaction, "host", int(app["id"]), delete=True)
        await interaction.response.send_message(f"{result} Channel: <#{app['application_channel_id']}>", ephemeral=True)

    @app_commands.command(name="insurance_app_delete_user", description="Delete open insurance app by user")
    async def insurance_app_delete_user(self, interaction: discord.Interaction, user: discord.Member, confirm: str):
        if confirm != "DELETE":
            await interaction.response.send_message("confirm must be DELETE", ephemeral=True)
            return
        _, insurance_repo, _ = await self._repos()
        app = await insurance_repo.get_open_app(interaction.guild.id, user.id) if interaction.guild else None
        if not app:
            await interaction.response.send_message("No open insurance app for user.", ephemeral=True)
            return
        result = await self._close_or_delete(interaction, "insurance", int(app["id"]), delete=True)
        await interaction.response.send_message(f"{result} Channel: <#{app['application_channel_id']}>", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
