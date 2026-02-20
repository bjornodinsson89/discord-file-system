from __future__ import annotations

import json
import logging
import re
from typing import Any

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
        await self.cog.handle_deny_submit(interaction, self.app_type, self.app_id, str(self.reason.value).strip())


class GenericPersistentButtonsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for custom_id in ["ha:ans:0", "ia:ans:0", "ha:ok:0", "ha:no:0", "ha:cl:0", "ha:del:0", "ia:ok:0", "ia:no:0", "ia:cl:0", "ia:del:0"]:
            self.add_item(discord.ui.Button(style=discord.ButtonStyle.secondary, label="_", custom_id=custom_id))


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(GenericPersistentButtonsView())

    async def cog_load(self) -> None:
        self.bot.add_listener(self.on_interaction, "on_interaction")

    async def _repos(self):
        db = get_database()
        return HostAppsRepository(db.pool), InsuranceAppsRepository(db.pool), GuildSettingsRepository(db)

    @staticmethod
    def _sanitize(text: str) -> str:
        value = re.sub(r"[^a-z0-9-]+", "-", text.lower())
        return re.sub(r"-+", "-", value).strip("-")[:70] or "applicant"

    @staticmethod
    def _questions_for(app_type: str) -> list[str]:
        return HOST_QUESTIONS if app_type == "host" else INSURER_QUESTIONS

    @staticmethod
    def _label_for(app_type: str) -> str:
        return "Host" if app_type == "host" else "Insurance"

    @staticmethod
    def _coerce_answers(raw: Any) -> dict[str, str]:
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items()}
            except Exception:
                return {}
        return {}

    @staticmethod
    def _trim(value: str, *, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 1] + "…"

    def _build_admin_summary_embed(
        self,
        app_type: str,
        app_id: int,
        applicant: discord.abc.User | discord.Member | None,
        status: str,
        questions: list[str],
        answers: dict[str, str],
    ) -> discord.Embed:
        applicant_id = int(getattr(applicant, "id", 0) or 0)
        applicant_text = f"<@{applicant_id}> ({applicant_id})" if applicant_id else "Unknown"
        embed = discord.Embed(title=f"{self._label_for(app_type)} Application #{app_id}", color=discord.Color.blurple())
        embed.add_field(name="Applicant", value=applicant_text, inline=False)
        embed.add_field(name="Status", value=status, inline=False)

        for idx, question in enumerate(questions, start=1):
            question_name = self._trim(f"Q{idx}: {question}", max_len=80)
            question_name = self._trim(question_name, max_len=256)
            answer = answers.get(f"q{idx}")
            value = "— Pending —" if not answer else self._trim(answer, max_len=900)
            embed.add_field(name=question_name, value=value, inline=False)
        return embed

    def _build_admin_inbox_view(self, app_type: str, app_id: int, status: str, jump_url: str) -> discord.ui.View:
        prefix = "ha" if app_type == "host" else "ia"
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Open", style=discord.ButtonStyle.link, url=jump_url, row=0))
        if status == "submitted":
            view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"{prefix}:ok:{app_id}", row=0))
            view.add_item(discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, custom_id=f"{prefix}:no:{app_id}", row=0))
            view.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id=f"{prefix}:del:{app_id}", row=0))
        else:
            view.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id=f"{prefix}:cl:{app_id}", row=0))
            view.add_item(discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger, custom_id=f"{prefix}:del:{app_id}", row=0))
        return view

    def _build_answer_view(self, app_type: str, app_id: int) -> discord.ui.View:
        prefix = "ha" if app_type == "host" else "ia"
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Answer", style=discord.ButtonStyle.primary, custom_id=f"{prefix}:ans:{app_id}"))
        return view

    async def _get_or_fetch_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    def _resolve_admin_inbox_channel(self, guild: discord.Guild, settings: dict[str, Any], app_type: str) -> discord.TextChannel | None:
        channel_id = settings.get("applications_admin_inbox_channel_id")
        if not channel_id:
            fallback_key = "host_apps_admin_inbox_channel_id" if app_type == "host" else "insurance_apps_admin_inbox_channel_id"
            channel_id = settings.get(fallback_key)
        try:
            channel = guild.get_channel(int(channel_id or 0))
        except (TypeError, ValueError):
            return None
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _ensure_admin_inbox_message(self, guild: discord.Guild, app_type: str, app: dict[str, Any]) -> int | None:
        host_repo, insurance_repo, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(guild.id)
        inbox_key = "host_apps_admin_inbox_channel_id" if app_type == "host" else "insurance_apps_admin_inbox_channel_id"
        channel_id = settings.get(inbox_key) or settings.get("applications_admin_inbox_channel_id")
        try:
            inbox = guild.get_channel(int(channel_id or 0))
        except (TypeError, ValueError):
            return None
        if not isinstance(inbox, discord.TextChannel):
            return None

        me = guild.me
        if me is None:
            return None
        perms = inbox.permissions_for(me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return None

        msg_id = int(app.get("admin_inbox_message_id") or 0)
        if msg_id:
            try:
                await inbox.fetch_message(msg_id)
                return msg_id
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        applicant = await self._get_or_fetch_member(guild, int(app.get("applicant_discord_id") or 0))
        embed = self._build_admin_summary_embed(
            app_type,
            int(app["id"]),
            applicant,
            str(app.get("status") or "in_progress"),
            self._questions_for(app_type),
            self._coerce_answers(app.get("answers")),
        )
        jump_url = f"https://discord.com/channels/{guild.id}/{int(app.get('application_channel_id') or 0)}"
        view = self._build_admin_inbox_view(app_type, int(app["id"]), str(app.get("status") or "in_progress"), jump_url)
        message = await inbox.send(
            content=f"**New {self._label_for(app_type)} Application #{int(app['id'])}** — <#{int(app.get('application_channel_id') or 0)}>",
            embed=embed,
            view=view,
        )
        repo = host_repo if app_type == "host" else insurance_repo
        await repo.set_admin_inbox_message_id(int(app["id"]), int(message.id))
        return int(message.id)

    async def _update_admin_panel(self, guild: discord.Guild, app_type: str, app: dict[str, Any]) -> None:
        msg_id = await self._ensure_admin_inbox_message(guild, app_type, app)
        if msg_id is None:
            return

        _, _, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(guild.id)
        inbox = self._resolve_admin_inbox_channel(guild, settings, app_type)
        if not isinstance(inbox, discord.TextChannel):
            return
        try:
            message = await inbox.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        applicant = await self._get_or_fetch_member(guild, int(app.get("applicant_discord_id") or 0))
        embed = self._build_admin_summary_embed(
            app_type,
            int(app["id"]),
            applicant,
            str(app.get("status") or "in_progress"),
            self._questions_for(app_type),
            self._coerce_answers(app.get("answers")),
        )
        jump_url = f"https://discord.com/channels/{guild.id}/{int(app.get('application_channel_id') or 0)}"
        view = self._build_admin_inbox_view(app_type, int(app["id"]), str(app.get("status") or "in_progress"), jump_url)
        await message.edit(embed=embed, view=view)

    async def _delete_admin_panel_message(self, guild: discord.Guild, app_type: str, app: dict[str, Any]) -> None:
        _, _, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(guild.id)
        inbox = self._resolve_admin_inbox_channel(guild, settings, app_type)
        if not isinstance(inbox, discord.TextChannel):
            return
        msg_id = int(app.get("admin_inbox_message_id") or 0)
        if not msg_id:
            return
        try:
            message = await inbox.fetch_message(msg_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def _is_admin(self, member: discord.Member, guild_id: int) -> bool:
        if member.guild_permissions.administrator:
            return True
        _, _, settings_repo = await self._repos()
        settings = await settings_repo.get_or_create(guild_id)
        allowed = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
        return bool(allowed.intersection({r.id for r in member.roles}))

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
                f"You already have an open application: <#{int(open_app['application_channel_id'])}>",
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

        await interaction.response.defer(ephemeral=True)

        overwrites: dict[Any, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        for role_id in GuildSettingsRepository.resolve_admin_role_ids(settings):
            role = interaction.guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        prefix = "host-app" if app_type == "host" else "insurer-app"
        app_channel = await interaction.guild.create_text_channel(
            name=f"{prefix}-{self._sanitize(interaction.user.display_name)}",
            category=category,
            overwrites=overwrites,
            reason="Application channel",
        )

        app = await repo.create_app(interaction.guild.id, interaction.user.id, app_channel.id)
        app_id = int(app["id"])

        inbox = self._resolve_admin_inbox_channel(interaction.guild, settings, app_type)
        if inbox is None:
            logger.warning(
                "Missing applications inbox channel for guild %s app_type %s app_id %s",
                interaction.guild.id,
                app_type,
                app_id,
            )
            try:
                await interaction.followup.send(
                    "Heads up: admins haven’t set the **Applications admin inbox channel** in /setup yet, so admins may not see your application until it’s configured.",
                    ephemeral=True,
                )
            except Exception:
                pass

        if isinstance(inbox, discord.TextChannel):
            embed = self._build_admin_summary_embed(
                app_type,
                app_id,
                interaction.user,
                str(app.get("status") or "in_progress"),
                self._questions_for(app_type),
                self._coerce_answers(app.get("answers")),
            )
            view = self._build_admin_inbox_view(app_type, app_id, str(app.get("status") or "in_progress"), app_channel.jump_url)
            msg = await inbox.send(
                content=f"**New {self._label_for(app_type)} Application #{app_id}** — {app_channel.mention}",
                embed=embed,
                view=view,
            )
            await repo.set_admin_inbox_message_id(app_id, int(msg.id))

        await app_channel.send(f"Application #{app_id} — {self._label_for(app_type)} for {interaction.user.mention}")
        await app_channel.send(
            f"Application #{app_id} — Q1/5: {self._questions_for(app_type)[0]}",
            view=self._build_answer_view(app_type, app_id),
        )
        await interaction.followup.send(f"Application started: {app_channel.mention}", ephemeral=True)

    async def submit_answer(self, interaction: discord.Interaction, app_type: str, app_id: int, current_question: int, answer: str) -> None:
        try:
            await interaction.response.defer()
            host_repo, insurance_repo, _ = await self._repos()
            repo = host_repo if app_type == "host" else insurance_repo
            current = await repo.get_by_id(app_id)
            if not current or current.get("status") != "in_progress":
                return
            if int(current.get("applicant_discord_id") or 0) != interaction.user.id:
                return
            updated = await repo.advance_answer(app_id, current_question, answer)
            if not updated:
                return
            if not interaction.guild:
                return
            channel = interaction.guild.get_channel(int(updated.get("application_channel_id") or 0))
            if not isinstance(channel, discord.TextChannel):
                return
            if updated.get("status") == "submitted":
                await channel.send("✅ Submitted for review.")
            else:
                next_question = int(updated.get("current_question") or 1)
                question_text = self._questions_for(app_type)[next_question - 1]
                await channel.send(
                    f"Application #{app_id} — Q{next_question}/5: {question_text}",
                    view=self._build_answer_view(app_type, app_id),
                )
            await self._update_admin_panel(interaction.guild, app_type, updated)
        except Exception:
            logger.exception("Failed to submit application answer")
            if interaction.response.is_done():
                await interaction.followup.send("Failed to save answer.", ephemeral=True)
            else:
                await interaction.response.send_message("Failed to save answer.", ephemeral=True)

    async def _close_application(self, guild: discord.Guild, app_type: str, app_id: int, reviewer_id: int) -> None:
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        updated = await repo.close_app(app_id, reviewer_id)
        if updated:
            await self._update_admin_panel(guild, app_type, updated)

    async def _delete_application(self, guild: discord.Guild, app_type: str, app: dict[str, Any]) -> None:
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        channel_id = int(app.get("application_channel_id") or 0)
        channel = guild.get_channel(channel_id)
        if channel is None and channel_id:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if isinstance(channel, discord.abc.GuildChannel):
            try:
                await channel.delete(reason=f"{app_type} application #{app['id']} deleted")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        await repo.delete_app(int(app["id"]))
        await self._delete_admin_panel_message(guild, app_type, app)

    async def _approve_application(self, interaction: discord.Interaction, app_type: str, app: dict[str, Any]) -> None:
        if not interaction.guild:
            return
        host_repo, insurance_repo, settings_repo = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        settings = await settings_repo.get_or_create(interaction.guild.id)
        role_key = "host99k_role_id" if app_type == "host" else "insurer_role_id"
        role_id = int(settings.get(role_key) or 0)
        if not role_id:
            await interaction.followup.send("Application role is not configured.", ephemeral=True)
            return

        member = await self._get_or_fetch_member(interaction.guild, int(app["applicant_discord_id"]))
        role = interaction.guild.get_role(role_id)
        if member is None or role is None:
            await interaction.followup.send("Applicant or configured role was not found.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason=f"{app_type} application #{app['id']} approved")
        except discord.Forbidden:
            await interaction.followup.send("I cannot grant the configured role.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("Failed to grant the configured role.", ephemeral=True)
            return

        try:
            await member.send(f"✅ Your {self._label_for(app_type)} application (#{app['id']}) was approved. You were granted **{role.name}**.")
        except discord.Forbidden:
            pass

        channel_id = int(app.get("application_channel_id") or 0)
        channel = interaction.guild.get_channel(channel_id)
        if channel is None and channel_id:
            try:
                channel = await interaction.guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None
        if isinstance(channel, discord.abc.GuildChannel):
            try:
                await channel.delete(reason=f"{app_type} application #{app['id']} approved")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await repo.delete_app(int(app["id"]))
        await self._delete_admin_panel_message(interaction.guild, app_type, app)

    async def handle_deny_submit(self, interaction: discord.Interaction, app_type: str, app_id: int, reason: str) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if not await self._is_admin(interaction.user, interaction.guild.id):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo
        app = await repo.get_by_id(app_id)
        if not app or app.get("status") != "submitted":
            return

        member = await self._get_or_fetch_member(interaction.guild, int(app["applicant_discord_id"]))
        if member is not None:
            try:
                await member.send(
                    f"❌ Your {self._label_for(app_type)} application (#{app['id']}) was denied.\nReason: {reason}"
                )
            except discord.Forbidden:
                pass

        await self._delete_application(interaction.guild, app_type, app)

    @app_commands.command(name="apply_99k_host", description="Apply as a 99k host")
    async def apply_99k_host(self, interaction: discord.Interaction):
        await self._start(interaction, "host")

    @app_commands.command(name="apply_insurance_provider", description="Apply as insurance provider")
    async def apply_insurance_provider(self, interaction: discord.Interaction):
        await self._start(interaction, "insurance")

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id") or "")
        parts = custom_id.split(":")
        if len(parts) != 3:
            return
        prefix, action, raw_id = parts
        if prefix not in {"ha", "ia"}:
            return
        try:
            app_id = int(raw_id)
        except ValueError:
            return
        app_type = "host" if prefix == "ha" else "insurance"

        host_repo, insurance_repo, _ = await self._repos()
        repo = host_repo if app_type == "host" else insurance_repo

        if action == "ans":
            app = await repo.get_by_id(app_id)
            if not app or int(app.get("applicant_discord_id") or 0) != interaction.user.id:
                await interaction.response.send_message("Only the applicant can answer.", ephemeral=True)
                return
            await interaction.response.send_modal(AnswerModal(self, app_type, app_id, int(app.get("current_question") or 1)))
            return

        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        if not await self._is_admin(interaction.user, interaction.guild.id):
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        app = await repo.get_by_id(app_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return

        if action == "ok":
            if app.get("status") != "submitted":
                await interaction.response.send_message("Application must be submitted first.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await self._approve_application(interaction, app_type, app)
            return

        if action == "no":
            if app.get("status") != "submitted":
                await interaction.response.send_message("Application must be submitted first.", ephemeral=True)
                return
            await interaction.response.send_modal(DenyModal(self, app_type, app_id))
            return

        if action == "cl":
            await interaction.response.defer(ephemeral=True)
            await self._close_application(interaction.guild, app_type, app_id, interaction.user.id)
            return

        if action == "del":
            await interaction.response.defer(ephemeral=True)
            await self._delete_application(interaction.guild, app_type, app)
            return


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
