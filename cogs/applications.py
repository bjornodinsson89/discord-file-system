from __future__ import annotations

import logging
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

from repositories.applications import ApplicationsRepository
from repositories.users import UsersRepository
from utils import GuildSettingsRepository, get_database

log = logging.getLogger("happy_jumper.applications")

HOST_APP_TYPE = "99k_host"
INSURER_APP_TYPE = "insurer"
ROLE_NAME_HOST = "99k_Jump_Host"
ROLE_NAME_INSURER = "HJ_Insureance_provider"
MIN_COVERAGE_DURATION_MINUTES = 30
MAX_COVERAGE_DURATION_MINUTES = 43200

HOST_QUESTIONS = [
    "Q1) What is your timezone? (example: MST)",
    "Q2) What are your availability windows? (example: Weeknights 7–11pm)",
    "Q3) What is your jumps-per-week capacity? (example: 2–3)",
    "Q4) Reliability agreement (Type: Yes)",
    "Q5) What is your backup plan if you cannot host as scheduled?",
]

INSURER_QUESTIONS = [
    "Q1) What is your timezone?",
    "Q2) What is your typical response time?",
    "Q3) Describe your coverage scope.",
    "Q4) Describe your pricing model (tiered/flat/other).",
    "Q5) What are your top denial reasons?",
]

INSURER_WIZARD_STEPS = [
    "Step 1/5 — Provide your display name for your Insurance Info Card.",
    "Step 2/5 — Provide your coverage summary. Note: Bot auto-detects ODs during coverage window and notifies you.",
    "Step 3/5 — Provide your pricing text (tables are okay as text).",
    "Step 4/5 — Provide your rules/exclusions (bullets encouraged).",
    "Step 5/5 — Send three lines in one message:\nactivation_delay_minutes: <int>\ncoverage_duration_minutes: <int>\nimage_url: <optional https image URL>\n(Use blank image_url to skip)",
]


def _is_valid_image_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    return parsed.path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))


def _is_admin_member(member: discord.Member, settings: dict[str, Any]) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    admin_role_ids = set(GuildSettingsRepository.resolve_admin_role_ids(settings))
    member_role_ids = {r.id for r in member.roles}
    return bool(admin_role_ids.intersection(member_role_ids))


def _sanitize_channel_name(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return (value or "application")[:90]


class DenyModal(discord.ui.Modal, title="Deny Application"):
    denial_reason = discord.ui.TextInput(label="Denial reason", style=discord.TextStyle.paragraph, required=True, max_length=2000)

    def __init__(self, cog: "ApplicationsCog", app_id: int):
        super().__init__()
        self.cog = cog
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_deny(interaction, self.app_id, str(self.denial_reason.value).strip())


class RequestChangesModal(discord.ui.Modal, title="Request Changes"):
    question_numbers = discord.ui.TextInput(label="Which question numbers need changes?", placeholder="Q2,Q5", required=True, max_length=50)
    notes = discord.ui.TextInput(label="Notes to applicant", style=discord.TextStyle.paragraph, required=True, max_length=2000)

    def __init__(self, cog: "ApplicationsCog", app_id: int):
        super().__init__()
        self.cog = cog
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_request_changes(
            interaction,
            self.app_id,
            str(self.question_numbers.value).strip(),
            str(self.notes.value).strip(),
        )


class ApplicationReviewView(discord.ui.View):
    def __init__(self, cog: "ApplicationsCog", app_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.app_id = app_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.handle_approve(interaction, self.app_id)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(DenyModal(self.cog, self.app_id))

    @discord.ui.button(label="Request Changes", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def request_changes(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(RequestChangesModal(self.cog, self.app_id))




class ApplicationAnswerModal(discord.ui.Modal):
    answer_text = discord.ui.TextInput(label="Your answer", style=discord.TextStyle.paragraph, required=True, max_length=1000)

    def __init__(self, cog: "ApplicationsCog", app_id: int):
        super().__init__(title=f"Application #{app_id}")
        self.cog = cog
        self.app_id = app_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_application_answer(interaction, self.app_id, str(self.answer_text.value).strip())


class ApplicationQuestionView(discord.ui.View):
    def __init__(self, cog: "ApplicationsCog", app_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.app_id = app_id

    @discord.ui.button(label="Answer", style=discord.ButtonStyle.primary, custom_id="app_answer")
    async def answer(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(ApplicationAnswerModal(self.cog, self.app_id))

class InsurerWizardStepModal(discord.ui.Modal):
    wizard_value = discord.ui.TextInput(label="Response", style=discord.TextStyle.paragraph, required=True, max_length=4000)

    def __init__(self, cog: "ApplicationsCog", guild_id: int, user_id: int, step: int):
        super().__init__(title=f"Insurance Card — Step {step + 1}/5")
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.step = step
        labels = ["Display name", "Coverage summary", "Pricing text", "Rules / exclusions"]
        placeholders = [
            "e.g. Falcon Insurance",
            "Describe what you cover and how it works",
            "Describe your pricing model",
            "List your exclusions/rules",
        ]
        self.wizard_value.label = labels[step]
        self.wizard_value.placeholder = placeholders[step]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = str(self.wizard_value.value).strip()
        if not value:
            await interaction.response.send_message("This field is required.", ephemeral=True)
            return
        await self.cog._advance_insurer_wizard(
            interaction=interaction,
            guild_id=self.guild_id,
            user_id=self.user_id,
            step=self.step,
            step_data={
                ["display_name", "coverage_summary", "pricing_text", "rules_exclusions"][self.step]: value,
            },
        )


class InsurerWizardTimingModal(discord.ui.Modal, title="Insurance Card — Step 5/5"):
    activation_delay_minutes = discord.ui.TextInput(label="activation_delay_minutes", style=discord.TextStyle.short, required=True, max_length=10)
    coverage_duration_minutes = discord.ui.TextInput(label="coverage_duration_minutes", style=discord.TextStyle.short, required=True, max_length=10)
    image_url = discord.ui.TextInput(label="image_url (optional)", style=discord.TextStyle.short, required=False, max_length=500)

    def __init__(self, cog: "ApplicationsCog", guild_id: int, user_id: int, step: int):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.step = step

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            delay = int(str(self.activation_delay_minutes.value).strip())
            duration = int(str(self.coverage_duration_minutes.value).strip())
        except ValueError:
            await interaction.response.send_message("activation_delay_minutes and coverage_duration_minutes must be integers.", ephemeral=True)
            return

        image_url = str(self.image_url.value).strip()
        if delay < 0:
            await interaction.response.send_message("activation_delay_minutes must be >= 0.", ephemeral=True)
            return
        if duration < MIN_COVERAGE_DURATION_MINUTES or duration > MAX_COVERAGE_DURATION_MINUTES:
            await interaction.response.send_message(
                f"coverage_duration_minutes must be between {MIN_COVERAGE_DURATION_MINUTES} and {MAX_COVERAGE_DURATION_MINUTES}.",
                ephemeral=True,
            )
            return
        if image_url and not _is_valid_image_url(image_url):
            await interaction.response.send_message("image_url must be https and end with png/jpg/jpeg/webp.", ephemeral=True)
            return

        await self.cog._advance_insurer_wizard(
            interaction=interaction,
            guild_id=self.guild_id,
            user_id=self.user_id,
            step=self.step,
            step_data={
                "activation_delay_minutes": delay,
                "coverage_duration_minutes": duration,
                "image_url": image_url or None,
            },
        )


class InsurerWizardView(discord.ui.View):
    def __init__(self, guild_id: int, target_user_id: int, cog: "ApplicationsCog"):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.target_user_id = target_user_id
        self.cog = cog

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_user_id:
            await interaction.response.send_message("This isn't your wizard.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary)
    async def continue_wizard(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        state = await repo.get_active_wizard_state_for_user(user_id=self.target_user_id)
        if not state or int(state.get("guild_id") or 0) != self.guild_id:
            await interaction.response.send_message("No active wizard. Run /insurer_card_setup again.", ephemeral=True)
            return
        step = int(state.get("step") or 0)
        if step >= 4:
            await interaction.response.send_modal(InsurerWizardTimingModal(self.cog, self.guild_id, self.target_user_id, step))
            return
        await interaction.response.send_modal(InsurerWizardStepModal(self.cog, self.guild_id, self.target_user_id, step))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_wizard(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not await self._guard(interaction):
            return
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        await repo.clear_wizard_state(guild_id=self.guild_id, user_id=self.target_user_id)
        await interaction.response.send_message("Wizard cancelled.", ephemeral=True)


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_stale_applications.start()

    def cog_unload(self):
        self.expire_stale_applications.cancel()

    async def _fetch_torn_identity(self, discord_id: int) -> dict[str, Any] | None:
        try:
            db = get_database()
            key_row = await UsersRepository(db.pool).get_user_api_key(discord_id)
            if not key_row or not key_row.get("torn_user_id"):
                return None
            torn_name = key_row.get("torn_name") or key_row.get("torn_username")
            return {"torn_user_id": key_row.get("torn_user_id"), "torn_name": torn_name or "Linked User"}
        except Exception:
            log.exception("Failed loading Torn identity for discord_id=%s", discord_id)
            return None

    def _normalize_answers(self, answers: object) -> dict[str, Any]:
        if answers is None:
            return {}
        if isinstance(answers, dict):
            return answers
        if isinstance(answers, str):
            stripped = answers.strip()
            if not stripped:
                return {}
            try:
                parsed = json.loads(stripped)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    async def _start_application(self, interaction: discord.Interaction, app_type: str, label: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        db = get_database()
        settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
        applications_channel_id = settings.get("applications_channel_id")
        if not applications_channel_id:
            await interaction.response.send_message("Admin must run /setup and set Applications Channel.", ephemeral=True)
            return

        identity = await self._fetch_torn_identity(interaction.user.id)
        if not identity:
            await interaction.response.send_message("Link your Torn API key first using /api", ephemeral=True)
            return

        repo = ApplicationsRepository(db.pool)
        existing = await repo.get_open_application(guild_id=interaction.guild.id, user_id=interaction.user.id, app_type=app_type)
        if existing:
            await interaction.response.send_message("You already have an open application of this type.", ephemeral=True)
            return

        parent = interaction.guild.get_channel(int(applications_channel_id))
        if not isinstance(parent, discord.TextChannel):
            await interaction.response.send_message("Applications Channel is not available. Please ask an admin to reconfigure /setup.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        channel_name = _sanitize_channel_name(f"{'99k-host' if app_type == HOST_APP_TYPE else 'insurer'}-app-{interaction.user.display_name}")

        bot_member = interaction.guild.me
        if not bot_member:
            await interaction.followup.send("Bot is not ready in this server yet.", ephemeral=True)
            return

        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for role_id in GuildSettingsRepository.resolve_admin_role_ids(settings):
            role = interaction.guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)

        try:
            app_channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=parent.category,
                overwrites=overwrites,
                reason="Application started",
            )
        except Exception:
            log.exception("Failed creating application channel guild_id=%s user_id=%s app_type=%s", interaction.guild.id, interaction.user.id, app_type)
            await interaction.followup.send("Could not start your application because channel creation failed. Please try again or contact an admin.", ephemeral=True)
            return

        app_id = await repo.create_application(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            app_type=app_type,
            application_channel_id=app_channel.id,
            thread_id=None,
            channel_id=parent.id,
            summary_message_id=None,
        )
        app_type_label = "99k Jump Host" if app_type == HOST_APP_TYPE else "Insurer"
        await parent.send(f"Application #{app_id} ({app_type_label}) started for {interaction.user.mention} — {app_channel.mention}")

        app = await repo.get_application_by_id(app_id)
        answers = self._normalize_answers((app or {}).get("answers"))
        summary_embed = self._build_summary_embed(interaction.user, identity, app_type, answers, app_id)
        summary_message = await app_channel.send(embed=summary_embed)
        try:
            await summary_message.pin(reason="Application Summary")
        except Exception:
            pass
        await repo.set_summary_message_id(app_id=app_id, message_id=summary_message.id)

        first_q = (HOST_QUESTIONS if app_type == HOST_APP_TYPE else INSURER_QUESTIONS)[0]
        await app_channel.send(f"Application #{app_id} — {first_q}", view=ApplicationQuestionView(self, app_id))
        await interaction.followup.send(f"Started your {label} application: {app_channel.mention}", ephemeral=True)

    def _build_summary_embed(self, user: discord.User | discord.Member, identity: dict[str, Any], app_type: str, answers: dict[str, Any], app_id: int) -> discord.Embed:
        answers = self._normalize_answers(answers)
        title = f"Application Summary — {ROLE_NAME_HOST if app_type == HOST_APP_TYPE else ROLE_NAME_INSURER}"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name="Application ID", value=f"#{app_id}", inline=False)
        embed.add_field(name="Applicant", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Torn", value=f"{identity.get('torn_name', 'Linked User')} [{identity.get('torn_user_id', 'N/A')}]", inline=False)
        questions = HOST_QUESTIONS if app_type == HOST_APP_TYPE else INSURER_QUESTIONS
        for idx, question in enumerate(questions, start=1):
            embed.add_field(name=f"Q{idx}", value=answers.get(f"q{idx}") or "(pending)", inline=False)
        return embed

    async def _update_summary_message(self, app_channel: discord.TextChannel, app: dict[str, Any], identity: dict[str, Any]):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app_id = int(app["id"])
        summary_message_id = app.get("summary_message_id")

        applicant = app_channel.guild.get_member(int(app["user_id"])) or self.bot.get_user(int(app["user_id"]))
        if applicant is None:
            applicant = self.bot.user
        embed = self._build_summary_embed(applicant, identity, app["app_type"], self._normalize_answers(app.get("answers")), app_id)

        target: discord.Message | None = None
        if summary_message_id:
            try:
                target = await app_channel.fetch_message(int(summary_message_id))
            except (discord.NotFound, discord.Forbidden):
                target = None

        if target is None:
            target = await app_channel.send(embed=embed)
            await repo.set_summary_message_id(app_id=app_id, message_id=target.id)
            app["summary_message_id"] = target.id
            try:
                await target.pin(reason="Application Summary")
            except Exception:
                pass

        try:
            await target.edit(embed=embed)
        except Exception:
            log.exception("applications_summary_update_error app_id=%s channel_id=%s message_id=%s", app_id, app_channel.id, target.id)

    async def _close_application_channel(self, app_channel: discord.TextChannel):
        try:
            await app_channel.set_permissions(app_channel.guild.default_role, send_messages=False)
        except Exception:
            log.exception("Failed updating channel permissions channel_id=%s", app_channel.id)

    @app_commands.command(name="apply_99k_host", description="Apply for 99k_Jump_Host")
    async def apply_99k_host(self, interaction: discord.Interaction):
        await self._start_application(interaction, HOST_APP_TYPE, ROLE_NAME_HOST)

    @app_commands.command(name="apply_insureance_provider", description="Apply for HJ_Insureance_provider")
    async def apply_insureance_provider(self, interaction: discord.Interaction):
        await self._start_application(interaction, INSURER_APP_TYPE, ROLE_NAME_INSURER)

    @app_commands.command(name="insurer_card_setup", description="Configure your HJ_Insureance_provider Insurance Info Card")
    async def insurer_card_setup(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        db = get_database()
        settings = await GuildSettingsRepository(db).get_or_create(interaction.guild.id)
        role_id = settings.get("insurer_role_id")
        has_role = any(r.id == int(role_id) for r in interaction.user.roles) if role_id else False
        approved = await ApplicationsRepository(db.pool).has_approved_insurer_application(guild_id=interaction.guild.id, user_id=interaction.user.id)
        if not has_role and not approved:
            await interaction.response.send_message(f"You need {ROLE_NAME_INSURER} role or approved application.", ephemeral=True)
            return

        started = await self.start_insurer_wizard(interaction.guild.id, interaction.user)
        if started:
            await interaction.response.send_message("I sent you a DM to continue the Insurance Info Card wizard.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "I couldn’t DM you. Enable DMs from server members and rerun /insurer_card_setup.",
                ephemeral=True,
            )

    async def start_insurer_wizard(self, guild_id: int, user: discord.User | discord.Member) -> bool:
        try:
            db = get_database()
            repo = ApplicationsRepository(db.pool)
            state = await repo.get_active_wizard_state_for_user(user_id=user.id)
            if state and int(state.get("guild_id") or 0) == guild_id:
                step = int(state.get("step") or 0)
                return await self._send_insurer_wizard_prompt(guild_id, user, step, resume=True)
            await repo.upsert_wizard_state(guild_id=guild_id, user_id=user.id, step=0, draft={})
            return await self._send_insurer_wizard_prompt(guild_id, user, 0, resume=False)
        except discord.Forbidden:
            return False
        except Exception:
            log.exception("Failed to start insurer wizard user_id=%s", user.id)
            return False

    async def _send_insurer_wizard_prompt(self, guild_id: int, user: discord.User | discord.Member, step: int, *, resume: bool) -> bool:
        dm = await user.create_dm()
        intro = f"Resuming at Step {step + 1}/5 for your {ROLE_NAME_INSURER} Insurance Info Card wizard." if resume else f"Starting {ROLE_NAME_INSURER} Insurance Info Card wizard."
        content = f"{intro}\n{INSURER_WIZARD_STEPS[step]}"
        await dm.send(content, view=InsurerWizardView(guild_id=guild_id, target_user_id=user.id, cog=self))
        return True

    async def _advance_insurer_wizard(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
        step: int,
        step_data: dict[str, Any],
    ) -> None:
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        state = await repo.get_active_wizard_state_for_user(user_id=user_id)
        if not state or int(state.get("guild_id") or 0) != guild_id:
            await interaction.response.send_message("No active wizard. Run /insurer_card_setup again.", ephemeral=True)
            return

        active_step = int(state.get("step") or 0)
        if active_step != step:
            await interaction.response.send_message("Wizard step changed. Please press Continue again.", ephemeral=True)
            return

        draft = state.get("draft") or {}
        draft.update(step_data)
        next_step = step + 1

        if next_step < len(INSURER_WIZARD_STEPS):
            await repo.upsert_wizard_state(guild_id=guild_id, user_id=user_id, step=next_step, draft=draft)
            await interaction.response.send_message("Saved. Check your DMs for the next step.", ephemeral=True)
            user = interaction.client.get_user(user_id) or interaction.user
            await self._send_insurer_wizard_prompt(guild_id, user, next_step, resume=False)
            return

        profile = await repo.upsert_insurer_profile(guild_id=guild_id, user_id=user_id, data=draft)
        await repo.clear_wizard_state(guild_id=guild_id, user_id=user_id)
        embed = discord.Embed(title=profile["display_name"], color=discord.Color.green())
        embed.add_field(name="Coverage summary", value=profile["coverage_summary"], inline=False)
        embed.add_field(name="Pricing", value=profile["pricing_text"], inline=False)
        embed.add_field(name="Rules/Exclusions", value=profile["rules_exclusions"], inline=False)
        embed.add_field(
            name="Coverage timing",
            value=f"Starts {profile['activation_delay_minutes']} minutes after payment verification, lasts {profile['coverage_duration_minutes']} minutes",
            inline=False,
        )
        embed.add_field(name="Claims", value="Auto-detected by the bot and forwarded to you", inline=False)
        if profile.get("image_url"):
            embed.set_image(url=profile["image_url"])
        await interaction.response.send_message("Saved. Check your DMs for your profile preview.", ephemeral=True)
        dm = await interaction.user.create_dm()
        await dm.send("Saved ✅", embed=embed)

        guild = self.bot.get_guild(guild_id)
        if guild:
            settings = await GuildSettingsRepository(db).get_or_create(guild_id)
            app_channel_id = settings.get("applications_channel_id")
            app_channel = guild.get_channel(int(app_channel_id)) if app_channel_id else None
            if isinstance(app_channel, discord.TextChannel):
                await app_channel.send(f"Provider Profile Updated: <@{user_id}>")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild is None or isinstance(message.channel, discord.DMChannel):
            try:
                await message.channel.send("This wizard uses buttons/modals now. Please click Continue in the wizard message.")
            except Exception:
                log.exception("dm guidance send failed")

    async def handle_application_answer(self, interaction: discord.Interaction, app_id: int, answer_text: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This action can only be used in a server.", ephemeral=True)
            return
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_application_by_id(app_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        if app.get("status") != "in_progress":
            await interaction.response.send_message("This application is no longer accepting answers.", ephemeral=True)
            return
        if int(app.get("user_id") or 0) != interaction.user.id:
            await interaction.response.send_message("Only the applicant can answer.", ephemeral=True)
            return
        if not answer_text:
            await interaction.response.send_message("Your answer cannot be empty.", ephemeral=True)
            return

        expected = int(app.get("current_question") or 0)
        next_status = "submitted" if expected >= 4 else None
        next_question = 5 if expected >= 4 else expected + 1
        updated = await repo.advance_question_if_current(
            app_id=int(app["id"]),
            expected_question=expected,
            answer_text=answer_text,
            next_status=next_status,
            next_question=next_question,
        )
        if not updated:
            await interaction.response.send_message("This question was already answered. Use the latest question prompt.", ephemeral=True)
            return

        app_channel = interaction.guild.get_channel(int(updated["application_channel_id"]))
        if not isinstance(app_channel, discord.TextChannel):
            await interaction.response.send_message("Application channel is not available.", ephemeral=True)
            return

        identity = await self._fetch_torn_identity(int(app["user_id"])) or {"torn_name": "Linked User", "torn_user_id": "N/A"}
        await self._update_summary_message(app_channel, updated, identity)

        if updated["status"] == "submitted":
            await app_channel.send("Submitted for review.", view=ApplicationReviewView(self, int(updated["id"])))
            await interaction.response.send_message("Answer recorded. Your application is now submitted.", ephemeral=True)
            return

        questions = HOST_QUESTIONS if updated["app_type"] == HOST_APP_TYPE else INSURER_QUESTIONS
        await app_channel.send(
            f"Application #{app_id} — {questions[next_question]}",
            view=ApplicationQuestionView(self, app_id),
        )
        await interaction.response.send_message("Answer recorded.", ephemeral=True)

    async def _admin_guard(self, interaction: discord.Interaction, app: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False, None
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild.id)
        if not _is_admin_member(interaction.user, settings):
            if interaction.response.is_done():
                await interaction.followup.send("Admin access required.", ephemeral=True)
            else:
                await interaction.response.send_message("Admin access required.", ephemeral=True)
            return False, None
        return True, settings

    async def handle_approve(self, interaction: discord.Interaction, app_id: int):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_application_by_id(app_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        ok, settings = await self._admin_guard(interaction, app)
        if not ok or settings is None:
            return
        updated = await repo.set_review(app_id=app_id, expected_status="submitted", new_status="approved", reviewed_by=interaction.user.id)
        if not updated:
            await interaction.response.send_message("Application is no longer pending review.", ephemeral=True)
            return
        await interaction.response.send_message("Approved.", ephemeral=True)
        app_channel = interaction.guild.get_channel(int(app["application_channel_id"]))
        role_id = settings.get("host99k_role_id") if app["app_type"] == HOST_APP_TYPE else settings.get("insurer_role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        applicant = interaction.guild.get_member(int(app["user_id"]))
        if applicant and role:
            try:
                await applicant.add_roles(role, reason="Application approved")
            except Exception:
                log.exception("Failed granting role app_id=%s", app_id)
        if isinstance(app_channel, discord.TextChannel):
            await app_channel.send(f"Approved by {interaction.user.mention}. Role granted: {ROLE_NAME_HOST if app['app_type'] == HOST_APP_TYPE else ROLE_NAME_INSURER}")

        if app["app_type"] == INSURER_APP_TYPE and applicant:
            try:
                started = await self.start_insurer_wizard(int(app["guild_id"]), applicant)
                if not started and isinstance(app_channel, discord.TextChannel):
                    await app_channel.send("Could not DM user; ask them to enable DMs and run /insurer_card_setup")
            except Exception:
                log.exception("Failed to start insurer wizard after approval app_id=%s", app_id)
                if isinstance(app_channel, discord.TextChannel):
                    await app_channel.send("Could not DM user; ask them to enable DMs and run /insurer_card_setup")

    async def handle_deny(self, interaction: discord.Interaction, app_id: int, reason: str):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_application_by_id(app_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        ok, _ = await self._admin_guard(interaction, app)
        if not ok:
            return
        updated = await repo.set_review(app_id=app_id, expected_status="submitted", new_status="denied", reviewed_by=interaction.user.id, denial_reason=reason)
        if not updated:
            await interaction.response.send_message("Application is no longer pending review.", ephemeral=True)
            return
        await interaction.response.send_message("Denied.", ephemeral=True)

        app_channel = interaction.guild.get_channel(int(app["application_channel_id"])) if interaction.guild else None
        applicant = interaction.guild.get_member(int(app["user_id"])) if interaction.guild else None
        if isinstance(app_channel, discord.TextChannel):
            await app_channel.send(f"Denied by {interaction.user.mention}.")
        if applicant:
            try:
                await applicant.send(f"Your application was denied. Reason: {reason}")
            except Exception:
                log.exception("Failed DM denial reason app_id=%s", app_id)

    async def handle_request_changes(self, interaction: discord.Interaction, app_id: int, q_numbers: str, notes: str):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_application_by_id(app_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        ok, _ = await self._admin_guard(interaction, app)
        if not ok:
            return

        nums = sorted({int(n) for n in re.findall(r"\d+", q_numbers) if 1 <= int(n) <= 5})
        if not nums:
            await interaction.response.send_message("Provide valid question numbers (Q1-Q5).", ephemeral=True)
            return
        restart_q = nums[0]
        updated = await repo.request_changes(app_id=app_id, current_question=restart_q - 1)
        if not updated:
            await interaction.response.send_message("Application is no longer pending review.", ephemeral=True)
            return
        await repo.trim_answers_from(app_id=app_id, from_question=restart_q)
        await interaction.response.send_message("Requested changes.", ephemeral=True)

        app_channel = interaction.guild.get_channel(int(app["application_channel_id"])) if interaction.guild else None
        if isinstance(app_channel, discord.TextChannel):
            await app_channel.send(f"<@{app['user_id']}> Please update {', '.join(f'Q{n}' for n in nums)}. Notes: {notes}")
            questions = HOST_QUESTIONS if app["app_type"] == HOST_APP_TYPE else INSURER_QUESTIONS
            await app_channel.send(f"Application #{app_id} — {questions[restart_q - 1]}", view=ApplicationQuestionView(self, app_id))


    async def _delete_application_with_channel(self, guild: discord.Guild, app: dict[str, Any]) -> bool:
        channel = guild.get_channel(int(app.get("application_channel_id") or 0))
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.delete(reason=f"Application #{app['id']} deleted by admin")
            except Exception:
                log.exception("Failed deleting application channel app_id=%s", app["id"])
                return False
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        return await repo.delete_application(int(app["id"]))

    @app_commands.command(name="application_delete", description="Delete an application by ID")
    async def application_delete(self, interaction: discord.Interaction, application_id: int, confirm: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild.id)
        if not _is_admin_member(interaction.user, settings):
            await interaction.response.send_message("Admin access required.", ephemeral=True)
            return
        if confirm != "DELETE":
            await interaction.response.send_message("Type DELETE to confirm.", ephemeral=True)
            return
        repo = ApplicationsRepository(get_database().pool)
        app = await repo.get_application_by_id(application_id)
        if not app:
            await interaction.response.send_message("Application not found.", ephemeral=True)
            return
        ok = await self._delete_application_with_channel(interaction.guild, app)
        if not ok:
            await interaction.response.send_message("Failed to delete application.", ephemeral=True)
            return
        await interaction.response.send_message(f"Deleted Application #{application_id}.", ephemeral=True)

    @app_commands.command(name="application_delete_user", description="Delete an open user application")
    @app_commands.describe(app_type="Application type")
    @app_commands.choices(app_type=[
        app_commands.Choice(name="host", value="host"),
        app_commands.Choice(name="insurer", value="insurer"),
    ])
    async def application_delete_user(self, interaction: discord.Interaction, user: discord.Member, app_type: app_commands.Choice[str], confirm: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild.id)
        if not _is_admin_member(interaction.user, settings):
            await interaction.response.send_message("Admin access required.", ephemeral=True)
            return
        if confirm != "DELETE":
            await interaction.response.send_message("Type DELETE to confirm.", ephemeral=True)
            return
        mapped_type = HOST_APP_TYPE if app_type.value == "host" else INSURER_APP_TYPE
        repo = ApplicationsRepository(get_database().pool)
        app = await repo.get_open_application(guild_id=interaction.guild.id, user_discord_id=user.id, app_type=mapped_type)
        if not app:
            await interaction.response.send_message("No open application found.", ephemeral=True)
            return
        ok = await self._delete_application_with_channel(interaction.guild, app)
        if not ok:
            await interaction.response.send_message("Failed to delete application.", ephemeral=True)
            return
        await interaction.response.send_message(f"Deleted Application #{app['id']}.", ephemeral=True)

    @tasks.loop(minutes=30)
    async def expire_stale_applications(self):
        try:
            db = get_database()
            repo = ApplicationsRepository(db.pool)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            rows = await repo.list_expired_candidates(older_than=cutoff)
            for app in rows:
                await repo.mark_expired(int(app["id"]))
                guild = self.bot.get_guild(int(app["guild_id"]))
                if not guild:
                    continue
                app_channel = guild.get_channel(int(app["application_channel_id"]))
                if isinstance(app_channel, discord.TextChannel):
                    await app_channel.send("Application expired after 48h of inactivity.")
        except Exception:
            log.exception("Failed expiring stale applications")

    @expire_stale_applications.before_loop
    async def before_expire_stale_applications(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
