from __future__ import annotations

import logging
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
        seed_message = await parent.send(f"Application Thread for {interaction.user.mention}")
        thread_name = f"{'99k-host' if app_type == HOST_APP_TYPE else 'insurer'}-app-{interaction.user.name}"[:90]
        thread = await seed_message.create_thread(name=thread_name, type=discord.ChannelType.private_thread)

        try:
            await thread.add_user(interaction.user)
        except Exception:
            log.exception("Failed adding applicant to thread thread_id=%s", thread.id)

        admin_role_ids = GuildSettingsRepository.resolve_admin_role_ids(settings)
        for role_id in admin_role_ids:
            role = interaction.guild.get_role(int(role_id))
            if not role:
                continue
            for member in role.members:
                try:
                    await thread.add_user(member)
                except Exception:
                    log.exception("Failed adding admin member to app thread member_id=%s thread_id=%s", member.id, thread.id)

        app = await repo.create_application(
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            app_type=app_type,
            thread_id=thread.id,
            channel_id=parent.id,
        )

        summary_embed = self._build_summary_embed(interaction.user, identity, app_type, {})
        summary_message = await thread.send(embed=summary_embed)
        await summary_message.pin(reason="Application Summary")

        first_q = HOST_QUESTIONS[0] if app_type == HOST_APP_TYPE else INSURER_QUESTIONS[0]
        await thread.send(first_q)
        await interaction.followup.send(f"Started your {label} application: {thread.mention}", ephemeral=True)

    def _build_summary_embed(self, user: discord.User | discord.Member, identity: dict[str, Any], app_type: str, answers: dict[str, Any]) -> discord.Embed:
        title = f"Application Summary — {ROLE_NAME_HOST if app_type == HOST_APP_TYPE else ROLE_NAME_INSURER}"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name="Applicant", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Torn", value=f"{identity.get('torn_name', 'Linked User')} [{identity.get('torn_user_id', 'N/A')}]", inline=False)
        questions = HOST_QUESTIONS if app_type == HOST_APP_TYPE else INSURER_QUESTIONS
        for idx, question in enumerate(questions, start=1):
            embed.add_field(name=f"Q{idx}", value=answers.get(f"q{idx}") or "(pending)", inline=False)
        return embed

    async def _update_summary_message(self, thread: discord.Thread, app: dict[str, Any], identity: dict[str, Any]):
        try:
            pins = await thread.pins()
            target = None
            for msg in pins:
                if msg.author.id == self.bot.user.id and msg.embeds and msg.embeds[0].title and "Application Summary" in msg.embeds[0].title:
                    target = msg
                    break
            if target is None:
                target = await thread.send("Application Summary")
                await target.pin(reason="Application Summary")
            applicant = thread.guild.get_member(int(app["user_id"])) or self.bot.get_user(int(app["user_id"]))
            if applicant is None:
                applicant = self.bot.user
            await target.edit(embed=self._build_summary_embed(applicant, identity, app["app_type"], app.get("answers") or {}))
        except Exception:
            log.exception("Failed to update summary app_id=%s", app.get("id"))

    async def _close_thread(self, thread: discord.Thread):
        try:
            await thread.edit(locked=True, archived=True)
        except Exception:
            log.exception("Failed to archive/lock thread_id=%s", thread.id)

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
        try:
            if isinstance(message.channel, discord.Thread) and message.guild:
                db = get_database()
                repo = ApplicationsRepository(db.pool)
                app = await repo.get_by_thread_id(message.channel.id)
                if not app or app.get("status") != "in_progress" or int(app.get("user_id")) != message.author.id:
                    return

                answer = (message.content or "").strip()
                if message.attachments:
                    urls = "\n".join(a.url for a in message.attachments)
                    answer = (answer + "\n" + urls).strip() if answer else urls
                if not answer:
                    return

                expected = int(app.get("current_question") or 0)
                next_status = "submitted" if expected >= 4 else None
                next_question = 5 if expected >= 4 else expected + 1
                updated = await repo.advance_question_if_current(
                    app_id=int(app["id"]),
                    expected_question=expected,
                    answer_text=answer,
                    next_status=next_status,
                    next_question=next_question,
                )
                if not updated:
                    return

                identity = await self._fetch_torn_identity(int(app["user_id"])) or {"torn_name": "Linked User", "torn_user_id": "N/A"}
                await self._update_summary_message(message.channel, updated, identity)

                if updated["status"] == "submitted":
                    await message.channel.send("Submitted for review.", view=ApplicationReviewView(self, int(app["id"])))
                else:
                    questions = HOST_QUESTIONS if app["app_type"] == HOST_APP_TYPE else INSURER_QUESTIONS
                    await message.channel.send(questions[next_question])
            elif message.guild is None or isinstance(message.channel, discord.DMChannel):
                await message.channel.send("This wizard uses buttons/modals now. Please click Continue in the wizard message.")
        except Exception:
            log.exception("on_message processing failed channel_id=%s", getattr(message.channel, "id", None))

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
        app = await repo.get_by_id(app_id)
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
        thread = interaction.guild.get_thread(int(app["thread_id"]))
        role_id = settings.get("host99k_role_id") if app["app_type"] == HOST_APP_TYPE else settings.get("insurer_role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        applicant = interaction.guild.get_member(int(app["user_id"]))
        if applicant and role:
            try:
                await applicant.add_roles(role, reason="Application approved")
            except Exception:
                log.exception("Failed granting role app_id=%s", app_id)
        if thread:
            await thread.send(f"Approved by {interaction.user.mention}. Role granted: {ROLE_NAME_HOST if app['app_type'] == HOST_APP_TYPE else ROLE_NAME_INSURER}")
            await self._close_thread(thread)

        if app["app_type"] == INSURER_APP_TYPE and applicant:
            try:
                started = await self.start_insurer_wizard(int(app["guild_id"]), applicant)
                if not started and thread:
                    await thread.send("Could not DM user; ask them to enable DMs and run /insurer_card_setup")
            except Exception:
                log.exception("Failed to start insurer wizard after approval app_id=%s", app_id)
                if thread:
                    await thread.send("Could not DM user; ask them to enable DMs and run /insurer_card_setup")

    async def handle_deny(self, interaction: discord.Interaction, app_id: int, reason: str):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_by_id(app_id)
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

        thread = interaction.guild.get_thread(int(app["thread_id"])) if interaction.guild else None
        applicant = interaction.guild.get_member(int(app["user_id"])) if interaction.guild else None
        if thread:
            await thread.send(f"Denied by {interaction.user.mention}.")
            await self._close_thread(thread)
        if applicant:
            try:
                await applicant.send(f"Your application was denied. Reason: {reason}")
            except Exception:
                log.exception("Failed DM denial reason app_id=%s", app_id)

    async def handle_request_changes(self, interaction: discord.Interaction, app_id: int, q_numbers: str, notes: str):
        db = get_database()
        repo = ApplicationsRepository(db.pool)
        app = await repo.get_by_id(app_id)
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

        thread = interaction.guild.get_thread(int(app["thread_id"])) if interaction.guild else None
        if thread:
            await thread.send(f"<@{app['user_id']}> Please update {', '.join(f'Q{n}' for n in nums)}. Notes: {notes}")

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
                thread = guild.get_thread(int(app["thread_id"]))
                if thread:
                    await thread.send("Application expired after 48h of inactivity.")
                    await self._close_thread(thread)
        except Exception:
            log.exception("Failed expiring stale applications")

    @expire_stale_applications.before_loop
    async def before_expire_stale_applications(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))
