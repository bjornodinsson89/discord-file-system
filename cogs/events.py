"""
Happy Jump Discord Bot - Discord-only service
Discord bot process entrypoint (no embedded web server).
"""

import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Optional

import config
from utils import init_database, get_database, init_torn_api, get_torn_api, init_security, get_security_manager, GuildSettingsRepository
from utils.database import get_pool
from utils.embeds import (
    create_success_embed, create_error_embed, create_warning_embed, create_info_embed,
    create_api_key_guide_embed, create_statistics_embed,
    create_raffle_embed, create_raffle_winner_embed, create_claim_notification_embed
)
from views import (
    ApiKeyIntroView, ConfirmRemoveKeyView, ApplicationReviewView, InsurerBrowserView
)
from utils.payouts import parse_payout_string, payout_items_to_human, PayoutParseError
from utils.torn_api import TornAPIError
from setup_panel import (
    DEFAULT_WELCOME_TEMPLATE,
    detect_rules_channel,
    has_setup_permission,
    render_welcome_template,
    send_setup_panel,
)
from cogs.pools import register_persistent_pool_views

from bot_actions import handlers as admin_handlers
from bot_actions.application_review import perform_application_review
from services import InsuranceService, DomainError, InvalidInput
from bot_actions.schemas import (
    CreateSessionRequest,
    CreateRaffleRequest,
    CreatePolicyRequest,
)

# REPOSITORY IMPORTS
from repositories.insurance import InsuranceRepository
from repositories.raffles import RafflesRepository
from repositories.audit import AuditRepository
from repositories.jumps import JumpsRepository
from repositories.users import UsersRepository
from repositories.torn_items import TornItemsRepository, norm_name
from services.payment_receipts import PaymentReceiptService

log = logging.getLogger("happy_jumper")

async def ensure_admin(interaction: discord.Interaction) -> bool:
    """Ensure invoking user can manage guild bot configuration/actions."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    member = interaction.user
    db = get_database()
    repo = GuildSettingsRepository(db)
    settings = await repo.get_or_create(interaction.guild.id)

    if has_setup_permission(
        member_id=member.id,
        guild_owner_id=interaction.guild.owner_id,
        is_administrator=member.guild_permissions.administrator,
        can_manage_guild=member.guild_permissions.manage_guild,
        member_role_ids={role.id for role in member.roles},
        admin_role_ids=GuildSettingsRepository.resolve_admin_role_ids(settings),
    ):
        return True

    embed = create_error_embed("Not Authorized", "Guild owner, Administrator, Manage Guild, or configured admin role required.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


async def assert99kHost(interaction: discord.Interaction, settings: dict | None) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not settings:
        return False

    member = interaction.user
    has_admin = bool(member.guild_permissions.administrator)
    host_role = settings.get("host_role_id")
    has_role = bool(host_role) and any(role.id == int(host_role) for role in member.roles)
    if has_admin and has_role:
        return True

    embed = create_error_embed("Clearance required.", "You need Administrator and the configured 99k Host role.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


class Jump99kSetupModal(discord.ui.Modal, title="99k Setup"):
    host_role_id = discord.ui.TextInput(label="Host Role ID", required=True, max_length=20)
    announce_channel_id = discord.ui.TextInput(label="Announce Channel ID (optional)", required=False, max_length=20)
    payee_discord_id = discord.ui.TextInput(label="Payee Discord ID (optional)", required=False, max_length=20)
    default_max_slots = discord.ui.TextInput(label="Default max slots", required=False, default="5", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(embed=create_error_embed("Clearance required.", "Administrator permission is required."), ephemeral=True)
            return
        try:
            role_id = int(str(self.host_role_id.value).strip())
            channel_id = int(str(self.announce_channel_id.value).strip()) if str(self.announce_channel_id.value).strip() else None
            payee_id = int(str(self.payee_discord_id.value).strip()) if str(self.payee_discord_id.value).strip() else None
            default_slots = int(str(self.default_max_slots.value).strip() or "5")
        except ValueError:
            await interaction.response.send_message(embed=create_error_embed("Invalid input", "Role/channel/payee IDs and slots must be numeric."), ephemeral=True)
            return

        db = get_database()
        repo = JumpsRepository(db.pool)
        await repo.upsert_settings(
            guild_id=interaction.guild_id,
            host_role_id=role_id,
            announce_channel_id=channel_id,
            payee_discord_id=payee_id,
            currency_default="cash",
            default_max_slots=default_slots,
        )
        await interaction.response.send_message(embed=create_success_embed("99k setup saved", "Settings updated for this guild."), ephemeral=True)



def _is_valid_torn_url(raw_url: str) -> bool:
    url = (raw_url or "").strip()
    if not url.lower().startswith("http"):
        return False
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return bool(host) and "torn.com" in host


def _excerpt(text: str, limit: int = 180) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


async def _resolve_announce_channel(interaction: discord.Interaction) -> discord.abc.Messageable | None:
    if not interaction.guild:
        return interaction.channel

    db = get_database()
    repo = GuildSettingsRepository(db)
    settings = await repo.get_or_create(interaction.guild.id)
    announce_channel_id = settings.get("announce_channel_id")
    if announce_channel_id:
        channel = interaction.guild.get_channel(int(announce_channel_id))
        if channel:
            return channel
    return interaction.channel


async def _can_review_applications(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    member = interaction.user
    db = get_database()
    repo = GuildSettingsRepository(db)
    settings = await repo.get_or_create(interaction.guild.id)

    admin_role_ids = GuildSettingsRepository.resolve_admin_role_ids(settings)

    if has_setup_permission(
        member_id=member.id,
        guild_owner_id=interaction.guild.owner_id,
        is_administrator=member.guild_permissions.administrator,
        can_manage_guild=member.guild_permissions.manage_guild,
        member_role_ids={role.id for role in member.roles},
        admin_role_ids=admin_role_ids,
    ):
        return True

    embed = create_error_embed("Not Authorized", "Configured admin role(s), Manage Guild, or Administrator required.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


class RequestInsurerModal(discord.ui.Modal, title="Insurer Application"):
    torn_id = discord.ui.TextInput(label="Torn ID", required=True, max_length=20)
    torn_name = discord.ui.TextInput(label="Torn Name", required=True, max_length=100)
    forum_url = discord.ui.TextInput(label="Torn Forum Thread URL", required=True, max_length=500)
    company_name = discord.ui.TextInput(label="Company/Service Name", required=False, max_length=100)
    description_terms_vouches = discord.ui.TextInput(
        label="Description/Terms + Proof/Vouches",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=3000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw_torn_id = str(self.torn_id.value).strip()
        torn_name = str(self.torn_name.value).strip()
        forum_url = str(self.forum_url.value).strip()
        company_name = str(self.company_name.value).strip() or None
        description_terms_vouches = str(self.description_terms_vouches.value).strip()

        try:
            torn_user_id = int(raw_torn_id)
        except ValueError:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid Torn ID", "Enter a valid numeric Torn ID (digits only, greater than 0)."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        db = get_database()
        try:
            provider = await InsuranceService(db).submit_insurer_application(
                guild_id=interaction.guild_id,
                discord_id=interaction.user.id,
                torn_user_id=torn_user_id,
                torn_name=torn_name,
                forum_url=forum_url,
                company_name=company_name,
                description_terms_vouches=description_terms_vouches,
            )
        except InvalidInput as exc:
            await interaction.followup.send(embed=create_error_embed("Invalid Application", str(exc)), ephemeral=True)
            return

        provider_id = provider["provider_id"]
        admin_channel = await _resolve_announce_channel(interaction)
        if admin_channel:
            review_embed = discord.Embed(title=f"insurer application #{provider_id} — Pending", color=discord.Color.blurple())
            review_embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            review_embed.add_field(name="Torn", value=f"`{raw_torn_id}` • **{torn_name}**", inline=False)
            review_embed.add_field(name="Forum URL", value=forum_url, inline=False)
            review_embed.add_field(name="Company", value=company_name or "N/A", inline=False)
            review_embed.add_field(name="Description excerpt", value=_excerpt(description_terms_vouches), inline=False)
            review_embed.set_footer(text="Use buttons below or /application_review")
            await admin_channel.send(
                embed=review_embed,
                view=ApplicationReviewView(
                    category="insurer",
                    application_id=provider_id,
                    applicant_discord_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                ),
            )

        await interaction.followup.send(
            embed=create_success_embed("Application Submitted", "Your insurer application was submitted and is pending admin review."),
            ephemeral=True,
        )


class RequestHost99kModal(discord.ui.Modal, title="Host 99k Application"):
    torn_id = discord.ui.TextInput(label="Torn ID", required=True, max_length=20)
    torn_name = discord.ui.TextInput(label="Torn Name", required=True, max_length=100)
    forum_url = discord.ui.TextInput(label="Hosting Thread/Forum URL", required=True, max_length=500)
    schedule = discord.ui.TextInput(label="Schedule/Availability", required=True, style=discord.TextStyle.paragraph, max_length=1000)
    experience_notes = discord.ui.TextInput(label="Experience + Notes", required=False, style=discord.TextStyle.paragraph, max_length=2000)

    async def on_submit(self, interaction: discord.Interaction):
        raw_torn_id = str(self.torn_id.value).strip()
        torn_name = str(self.torn_name.value).strip()
        forum_url = str(self.forum_url.value).strip()
        schedule = str(self.schedule.value).strip()
        experience_notes = str(self.experience_notes.value).strip()

        if not raw_torn_id.isdigit() or int(raw_torn_id) <= 0:
            await interaction.response.send_message(
                embed=create_error_embed("Invalid Torn ID", "Enter a valid numeric Torn ID (digits only, greater than 0)."),
                ephemeral=True,
            )
            return
        if not torn_name or not schedule:
            await interaction.response.send_message(embed=create_error_embed("Missing Fields", "Please complete all required fields."), ephemeral=True)
            return
        if not forum_url or not _is_valid_torn_url(forum_url):
            await interaction.response.send_message(
                embed=create_error_embed("Invalid URL", "Forum URL is required and must be a full Torn host thread URL (e.g., https://www.torn.com/...)."),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        db = get_database()
        application_data = {
            "schedule": schedule,
            "experience": experience_notes or None,
            "notes_rules": None,
        }
        fallback_name = (interaction.user.display_name or interaction.user.name or "").strip()
        effective_torn_name = torn_name or fallback_name
        host_application = await db.upsert_host_application(
            guild_id=interaction.guild_id,
            discord_id=interaction.user.id,
            torn_user_id=int(raw_torn_id),
            torn_name=effective_torn_name,
            display_name=interaction.user.display_name,
            forum_url=forum_url,
            application_data=application_data,
        )

        application_id = host_application["id"]
        admin_channel = await _resolve_announce_channel(interaction)
        if admin_channel:
            review_embed = discord.Embed(title=f"host99k application #{application_id} — Pending", color=discord.Color.blurple())
            review_embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
            review_embed.add_field(name="Torn", value=f"`{raw_torn_id}` • **{effective_torn_name}**", inline=False)
            review_embed.add_field(name="Forum URL", value=forum_url, inline=False)
            review_embed.add_field(name="Schedule excerpt", value=_excerpt(schedule), inline=False)
            review_embed.set_footer(text="Use buttons below or /application_review")
            await admin_channel.send(
                embed=review_embed,
                view=ApplicationReviewView(
                    category="host99k",
                    application_id=application_id,
                    applicant_discord_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                ),
            )

        await interaction.followup.send(
            embed=create_success_embed("Application Submitted", "Your host99k application was submitted and is pending admin review."),
            ephemeral=True,
        )


# ============================================================================
# BOT SETUP
# ============================================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.synced = False


async def sync_application_commands() -> None:
    """Sync commands in one scope only (global OR guild), with optional cleanup."""
    if bot.synced:
        log.info("Command sync skipped (already synced).")
        return

    db = get_database()
    lock_key = 82542001
    have_lock = await db.try_advisory_lock(lock_key)
    if not have_lock:
        log.info("Command sync skipped (another bot process currently syncing commands)")
        return

    try:
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            log.info("Command sync scope=guild:%s CLEAN_COMMANDS=%s", config.GUILD_ID, config.CLEAN_COMMANDS)

            if config.CLEAN_COMMANDS:
                bot.tree.clear_commands(guild=None)
                await bot.tree.sync()
                log.info("Cleanup: cleared GLOBAL commands")

                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
                log.info("Cleanup: cleared guild commands in %s", config.GUILD_ID)

            synced = await bot.tree.sync(guild=guild)
            log.info("Commands synced to guild %s: %s commands", config.GUILD_ID, len(synced))
        else:
            log.info("Command sync scope=global CLEAN_COMMANDS=%s", config.CLEAN_COMMANDS)

            if config.CLEAN_COMMANDS:
                for g in bot.guilds:
                    try:
                        bot.tree.clear_commands(guild=g)
                        await bot.tree.sync(guild=g)
                    except Exception:
                        log.exception("Cleanup: failed to clear guild commands for guild %s", g.id)
                bot.tree.clear_commands(guild=None)
                await bot.tree.sync()
                log.info("Cleanup: cleared GLOBAL commands before final sync")

            synced = await bot.tree.sync()
            log.info("Commands synced globally: %s commands", len(synced))

        bot.synced = True
    except Exception:
        log.exception("Failed to sync commands")
    finally:
        await db.release_advisory_lock(lock_key)


async def _send_interaction_error(interaction: discord.Interaction, message: str):
    """Best-effort user-facing interaction error response."""
    embed = create_error_embed("Error", message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception:
        log.exception("Failed to send interaction error response")


async def _global_view_error(self, interaction: discord.Interaction, error: Exception, item):
    log.exception("Unhandled UI interaction error (item=%s): %s", getattr(item, "custom_id", None), error)
    await _send_interaction_error(interaction, "An unexpected error occurred. Please try again.")


async def _global_modal_error(self, interaction: discord.Interaction, error: Exception):
    log.exception("Unhandled modal interaction error: %s", error)
    await _send_interaction_error(interaction, "An unexpected error occurred. Please try again.")


discord.ui.View.on_error = _global_view_error
discord.ui.Modal.on_error = _global_modal_error


async def setup_hook():
    """Initialize process-scoped dependencies once per bot lifecycle."""
    config.validate_config()
    await init_database()
    init_torn_api()
    await init_security()
    admin_handlers.set_bot_instance(bot)
    log.info("Process dependencies initialized")


bot.setup_hook = setup_hook


async def register_persistent_application_review_views() -> None:
    """Register persistent approve/deny views for pending applications."""
    db = get_database()

    insurer_apps = await db.list_pending_insurer_applications()
    for app in insurer_apps:
        guild_id = app.get("guild_id")
        if guild_id is None:
            continue
        bot.add_view(
            ApplicationReviewView(
                category="insurer",
                application_id=app["provider_id"],
                applicant_discord_id=app["discord_id"],
                guild_id=guild_id,
            )
        )

    host_apps = await db.list_pending_host_applications()
    for app in host_apps:
        bot.add_view(
            ApplicationReviewView(
                category="host99k",
                application_id=app["id"],
                applicant_discord_id=app["discord_id"],
                guild_id=app["guild_id"],
            )
        )

# ============================================================================
# BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot ready handler."""
    log.info(f"Bot logged in as {bot.user}")
    log.info(f"Bot ID: {bot.user.id}")
    log.info(f"Discord.py version: {discord.__version__}")
    log.info(f"Guilds: {len(bot.guilds)}")
    
    await sync_application_commands()
    await register_persistent_application_review_views()
    await register_persistent_pool_views(bot)

    # Start background workers
    if not cleanup_worker.is_running():
        cleanup_worker.start()
    if not readiness_worker.is_running():
        readiness_worker.start()
    if not insurance_monitor.is_running():
        insurance_monitor.start()
    if not raffle_completion_worker.is_running():
        raffle_completion_worker.start()
    
    log.info("✓ Bot is ready!")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Handle bot joining a new guild."""
    log.info(f"Joined guild: {guild.name} ({guild.id})")
    db = get_database()
    repo = GuildSettingsRepository(db)
    settings = await repo.get_or_create(guild.id)
    if settings.get("announce_channel_id"):
        return

    me = guild.me
    for channel in guild.text_channels:
        perms = channel.permissions_for(me) if me else None
        if perms and perms.send_messages and perms.embed_links:
            await repo.set_announce_channel(guild.id, channel.id)
            log.info("Auto-selected announce channel %s for guild %s", channel.id, guild.id)
            break


@bot.event
async def on_member_join(member: discord.Member):
    """Send configured welcome message when enabled for the guild."""
    if member.bot:
        return

    db = get_database()
    repo = GuildSettingsRepository(db)
    settings = await repo.get_guild_settings(member.guild.id)

    if not settings.get("welcome_enabled"):
        return

    welcome_channel_id = settings.get("welcome_channel_id")
    if not welcome_channel_id:
        return

    channel = member.guild.get_channel(int(welcome_channel_id))
    if channel is None:
        log.warning("Welcome channel %s not found in guild %s", welcome_channel_id, member.guild.id)
        return

    template = (settings.get("welcome_message_template") or "").strip()
    if not template:
        template = DEFAULT_WELCOME_TEMPLATE
        try:
            await repo.upsert_guild_settings(member.guild.id, welcome_message_template=DEFAULT_WELCOME_TEMPLATE)
        except Exception:
            log.exception("Failed to auto-save default welcome template for guild=%s", member.guild.id)

    rules_channel = detect_rules_channel(member.guild)
    message = render_welcome_template(template, member, rules_channel)

    try:
        await channel.send(message)
        log.info("Welcome message sent in guild=%s channel=%s user=%s", member.guild.id, channel.id, member.id)
    except (discord.Forbidden, discord.NotFound):
        log.warning("Unable to send welcome message in guild=%s channel=%s", member.guild.id, welcome_channel_id)
    except Exception:
        log.exception("Failed to send welcome message in guild=%s", member.guild.id)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global slash command error handler."""
    log.exception("Unhandled slash command error: %s", error)
    await _send_interaction_error(interaction, "An unexpected error occurred. Please try again.")


# ============================================================================
# SLASH COMMANDS - API KEY MANAGEMENT
# ============================================================================

@bot.tree.command(name="set_api_key", description="Register your Torn API key for bot features")
async def set_api_key(interaction: discord.Interaction):
    """Register or update user's Torn API key."""
    await interaction.response.defer(ephemeral=True)
    embed = create_api_key_guide_embed()
    view = ApiKeyIntroView()
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="remove_api_key", description="Delete your stored Torn API key")
async def remove_api_key(interaction: discord.Interaction):
    """Remove user's stored API key."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    existing = await db.get_user_api_key(interaction.user.id)
    
    if not existing:
        embed = create_error_embed("No API Key", "You don't have an API key registered.")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    
    view = ConfirmRemoveKeyView()
    embed = create_warning_embed(
        "Remove API Key?",
        "Are you sure you want to remove your API key? You will need to re-register to use bot features."
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="my_sessions", description="View your active jump sessions and waitlist positions")
async def my_sessions(interaction: discord.Interaction):
    """Show user's current sessions and waitlist entries."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    
    sessions = await db.get_active_sessions(interaction.guild_id)
    
    user_signups = []
    user_waitlist = []
    hosted_sessions = []
    
    for session in sessions:
        if session['host_discord_id'] == interaction.user.id:
            hosted_sessions.append(session)
        
        signup = await db.get_signup(session['id'], interaction.user.id)
        if signup:
            user_signups.append({'session': session, 'signup': signup})
        
        waitlist_pos = await db.get_waitlist_position(session['id'], interaction.user.id)
        if waitlist_pos:
            user_waitlist.append({'session': session, 'position': waitlist_pos})
    
    embed = create_info_embed(f"{config.EMOJI_JUMP} Your Sessions")
    
    if hosted_sessions:
        hosted_text = "\n".join([
            f"**Session #{s['id']}** - {s['status'].title()} ({s['xanax_count']}x Xanax, {s['max_spots']} spots)"
            for s in hosted_sessions
        ])
        embed.add_field(name="Hosting", value=hosted_text, inline=False)
    
    if user_signups:
        signups_text = "\n".join([
            f"**Session #{s['session']['id']}** - Status: {s['signup']['status'].title()}"
            for s in user_signups
        ])
        embed.add_field(name="Signed Up", value=signups_text, inline=False)
    
    if user_waitlist:
        waitlist_text = "\n".join([
            f"**Session #{w['session']['id']}** - Position #{w['position']}"
            for w in user_waitlist
        ])
        embed.add_field(name="Waitlist", value=waitlist_text, inline=False)
    
    if not (hosted_sessions or user_signups or user_waitlist):
        embed.description = "You're not currently in any active sessions or waitlists."
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# SLASH COMMANDS - ADMIN SETUP
# ============================================================================

@bot.tree.command(name="setup", description="Open the interactive server setup panel")
async def setup(interaction: discord.Interaction):
    db = get_database()
    await send_setup_panel(interaction, db)



@bot.tree.command(name="stats", description="View server statistics")
async def stats(interaction: discord.Interaction):
    """Show server statistics."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    stats = await db.get_guild_statistics(interaction.guild_id)
    
    embed = create_statistics_embed(stats, f"Statistics for {interaction.guild.name}")
    await interaction.followup.send(embed=embed, ephemeral=True)



# ============================================================================
# SLASH COMMANDS - ADMIN ACTIONS
# ============================================================================

@bot.tree.command(name="refresh_item_icons", description="Refresh Torn item icon index (Admin only)")
@app_commands.default_permissions(administrator=True)
async def refresh_item_icons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return

    db = get_database()
    row = await db.get_user_api_key(interaction.user.id)
    if not row:
        await interaction.followup.send(
            embed=create_error_embed("API Key Required", "Use the bot's API key setup first."),
            ephemeral=True,
        )
        return

    encrypted = row.get("encrypted_key") or row.get("api_key_encrypted")
    if not encrypted:
        await interaction.followup.send(
            embed=create_error_embed("API Key Required", "Use the bot's API key setup first."),
            ephemeral=True,
        )
        return

    try:
        api_key = get_security_manager().decrypt_api_key(encrypted)
    except Exception:
        log.exception("Failed to decrypt API key for user %s", interaction.user.id)
        await interaction.followup.send(
            embed=create_error_embed("API Key Error", "Stored API key could not be decrypted. Please set it again."),
            ephemeral=True,
        )
        return

    torn = get_torn_api()
    try:
        data = await torn.get_torn_items(api_key)
    except TornAPIError as e:
        await interaction.followup.send(
            embed=create_error_embed("Torn API Error", str(e)),
            ephemeral=True,
        )
        return
    except Exception as e:
        log.exception("Unexpected error fetching Torn items: %s", e)
        await interaction.followup.send(
            embed=create_error_embed("Refresh Failed", "Unexpected error fetching item data from Torn API."),
            ephemeral=True,
        )
        return

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, (dict, list)):
        top_level_keys = list(data.keys()) if isinstance(data, dict) else []
        details: list[str] = [
            "Unexpected Torn API response: missing or invalid `items` payload.",
            f"Top-level keys: `{top_level_keys}`",
        ]
        if isinstance(data, dict):
            api_error = data.get("message") or data.get("error")
            if api_error is not None:
                details.append(f"API message: `{str(api_error)[:300]}`")
        await interaction.followup.send(
            embed=create_error_embed("Refresh Failed", "\n".join(details)),
            ephemeral=True,
        )
        return

    def _pick_image_url(item_payload: dict, item_id: int) -> str:
        image_payload = item_payload.get("image")
        candidate = ""

        if isinstance(image_payload, dict):
            for key in ("large", "full", "preview", "medium", "small", "thumbnail"):
                value = image_payload.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    break
        elif isinstance(image_payload, str) and image_payload.strip():
            candidate = image_payload.strip()

        if candidate.startswith("//"):
            candidate = f"https:{candidate}"
        elif candidate.startswith("/"):
            candidate = f"https://www.torn.com{candidate}"

        if not candidate:
            candidate = f"https://www.torn.com/images/items/{item_id}/large.png"
        return candidate

    rows: list[tuple[int, str, str, str]] = []
    name_to_item_id: dict[str, int] = {}

    if isinstance(items, dict):
        item_entries = items.items()
    else:
        item_entries = [(None, item) for item in items]

    for id_key, item in item_entries:
        if not isinstance(item, dict):
            continue

        raw_name = item.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue

        item_id = item.get("id") or item.get("item_id") or item.get("ID")
        if item_id is None and id_key is not None:
            item_id = id_key

        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            continue

        name = raw_name.strip()
        normalized = norm_name(name)
        if not normalized:
            continue

        image_url = _pick_image_url(item, item_id)
        rows.append((item_id, name, normalized, image_url))
        if normalized not in name_to_item_id:
            name_to_item_id[normalized] = item_id

    if not rows:
        await interaction.followup.send(
            embed=create_error_embed("Refresh Failed", "No valid item rows were returned by Torn API."),
            ephemeral=True,
        )
        return

    repo = TornItemsRepository(db.pool)
    inserted = await repo.upsert_items(rows)

    alias_targets = {
        "xanax": "Xanax",
        "xan": "Xanax",
        "edvd": "Erotic DVD",
        "e dvd": "Erotic DVD",
        "e dv d": "Erotic DVD",
        "erotic dvd": "Erotic DVD",
        "ecstacy": "Ecstasy",
        "xtc": "Ecstasy",
        "dp": "Donator Pack",
        "donator pack": "Donator Pack",
    }
    aliases: dict[str, int] = {}
    for alias, target_name in alias_targets.items():
        target_norm = norm_name(target_name)
        target_id = name_to_item_id.get(target_norm)
        if target_id:
            aliases[norm_name(alias)] = int(target_id)

    alias_count = await repo.upsert_aliases(aliases)
    refreshed_iso = datetime.now(timezone.utc).isoformat()
    await repo.set_last_refresh_iso(refreshed_iso)

    await interaction.followup.send(
        embed=create_success_embed(
            "Item Icons Refreshed",
            f"Upserted items: **{inserted}**\nAliases updated: **{alias_count}**\nRefreshed at: `{refreshed_iso}`",
        ),
        ephemeral=True,
    )


class Jump99kSessionModal(discord.ui.Modal, title="✨ 99k Happy Jump ✨"):
    payment_type = discord.ui.TextInput(
        label="💰 Payment Type (💊 xanax | 📀 erotic_dvd)",
        required=True,
        max_length=20,
        placeholder="xanax or erotic_dvd",
    )
    max_slots = discord.ui.TextInput(label="🎟️ Max slots (1-5)", required=True, max_length=1, placeholder="1-5")
    spot_price = discord.ui.TextInput(
        label="💵 Spot Price (1-50)",
        required=True,
        max_length=4,
        placeholder="1-50",
    )
    possible_tct_start = discord.ui.TextInput(
        label="⏰ Possible TCT start time (24h HH:MM, minutes 00/15/30/45)",
        required=False,
        max_length=5,
        placeholder="e.g. 22:15",
    )
    notes = discord.ui.TextInput(label="📝 Notes (optional)", required=False, style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, settings: dict, session: dict | None = None):
        super().__init__()
        self.settings = settings
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        repo = JumpsRepository(get_pool())
        try:
            title = "✨ 99k Happy Jump ✨"
            try:
                slots = int(str(self.max_slots.value).strip())
            except ValueError:
                await interaction.response.send_message(embed=create_error_embed("Invalid max slots", "Max slots must be a number from 1 to 5."), ephemeral=True)
                return
            if slots < 1 or slots > 5:
                await interaction.response.send_message(embed=create_error_embed("Invalid max slots", "Max slots must be from 1 to 5."), ephemeral=True)
                return

            raw_payment_type = str(self.payment_type.value).strip().lower()
            if raw_payment_type == "edvd":
                raw_payment_type = "erotic_dvd"
            if raw_payment_type not in {"xanax", "erotic_dvd"}:
                await interaction.response.send_message(embed=create_error_embed("Invalid payment type", "Payment type must be one of: 💊 xanax, 📀 erotic_dvd."), ephemeral=True)
                return

            try:
                price_amount = int(str(self.spot_price.value).strip())
            except ValueError:
                await interaction.response.send_message(embed=create_error_embed("Invalid spot price", "Spot price must be a whole number from 1 to 50."), ephemeral=True)
                return
            if price_amount < 1 or price_amount > 50:
                await interaction.response.send_message(embed=create_error_embed("Invalid spot price", "Spot price must be between 1 and 50."), ephemeral=True)
                return

            scheduled = str(self.possible_tct_start.value).strip() or None
            if scheduled and not re.fullmatch(r"^([01]\d|2[0-3]):(00|15|30|45)$", scheduled):
                await interaction.response.send_message(embed=create_error_embed("Invalid start time", "Possible TCT start time must use 24h HH:MM and minutes 00/15/30/45 (example: 22:15)."), ephemeral=True)
                return

            notes = str(self.notes.value).strip() or None
            announce_channel_id = self.settings.get("announce_channel_id")
            if self.session:
                await repo.update_session(int(self.session["id"]), title=title, scheduled_start_text=scheduled, max_slots=slots, notes=notes, price_item=raw_payment_type, price_amount=price_amount)
                session_id = int(self.session["id"])
            else:
                session_id = await repo.create_session(
                    guild_id=interaction.guild_id,
                    host_discord_id=interaction.user.id,
                    title=title,
                    scheduled_start_text=scheduled,
                    max_slots=slots,
                    notes=notes,
                    price_item=raw_payment_type,
                    price_amount=price_amount,
                    announce_channel_id=announce_channel_id,
                    announce_message_id=None,
                )
            channel = interaction.guild.get_channel(int(announce_channel_id)) if interaction.guild and announce_channel_id else interaction.channel
            if channel:
                item_label = "Xanax" if raw_payment_type == "xanax" else "eDVD"
                start_line = f"Possible TCT start: **{scheduled}**\n" if scheduled else ""
                notes_line = f"Notes: {notes}\n" if notes else ""
                content = f"📣 **{title}** — Session **#{session_id}**\n{start_line}Spot price: **{price_amount}x {item_label}**\n{notes_line}Click to join."
                message_id = self.session.get("announce_message_id") if self.session else None
                if message_id:
                    try:
                        msg = await channel.fetch_message(int(message_id))
                        await msg.edit(content=content, view=Jump99kSignupView(session_id))
                    except Exception:
                        msg = await channel.send(content, view=Jump99kSignupView(session_id))
                        await repo.set_announcement_message(session_id, channel_id=channel.id, message_id=msg.id)
                else:
                    msg = await channel.send(content, view=Jump99kSignupView(session_id))
                    await repo.set_announcement_message(session_id, channel_id=channel.id, message_id=msg.id)
            verb = "updated" if self.session else "created"
            await interaction.response.send_message(embed=create_success_embed("99k session saved", f"Session #{session_id} {verb}."), ephemeral=True)
        except Exception as e:
            log.exception("99k session modal submit failed: %s", e)
            err = create_error_embed("99k start failed", f"{type(e).__name__}: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(embed=err, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err, ephemeral=True)


class Jump99kEditSelectModal(discord.ui.Modal, title="Edit 99k Session"):
    jump_id = discord.ui.TextInput(label="Jump ID", required=True, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        repo = JumpsRepository(get_pool())
        try:
            session_id = int(str(self.jump_id.value).strip())
        except ValueError:
            await interaction.response.send_message(embed=create_error_embed("Invalid Jump ID", "Jump ID must be numeric."), ephemeral=True)
            return
        session = await repo.get_session(session_id)
        if not session or int(session.get("guild_id")) != int(interaction.guild_id):
            await interaction.response.send_message(embed=create_error_embed("Not found", "Session not found for this server."), ephemeral=True)
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
        await interaction.response.send_modal(Jump99kSessionModal(settings, session=session))


jump99k_group = app_commands.Group(name="99k", description="99k happy jump commands")


@jump99k_group.command(name="start", description="Create a 99k jump session")
async def jump99k_start(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    await interaction.response.send_modal(Jump99kSessionModal(settings, session=None))


@jump99k_group.command(name="edit", description="Edit an open 99k jump session")
async def jump99k_edit(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    await interaction.response.send_modal(Jump99kEditSelectModal())


@jump99k_group.command(name="list", description="List 99k sessions and readiness")
async def jump99k_list(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    repo = JumpsRepository(get_pool())
    session = await repo.get_active_session(interaction.guild_id)
    if not session:
        await interaction.response.send_message(embed=create_info_embed("99k sessions", "No open sessions."), ephemeral=True)
        return
    rows = await repo.list_signups_with_readiness(int(session["id"]))
    lines = []
    for r in rows:
        green = int(r.get("energy") or 0) == 1000 and int(r.get("drug_cooldown") or 0) == 0
        color = "🟢" if green else "🔴"
        if r.get("overdose_flag"):
            color = "🟠"
        lines.append(f"{color} <@{r['discord_id']}> • E {r.get('energy') or 0}/{r.get('energy_max') or 0} • CD {r.get('drug_cooldown') or 0}s • {r.get('status_text') or 'unknown'}")
    if not lines:
        lines = ["No signups yet."]
    await interaction.response.send_message(embed=create_info_embed(f"99k Session #{session['id']}", "\n".join(lines)), ephemeral=True)


@jump99k_group.command(name="end", description="Close active 99k session")
async def jump99k_end(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    repo = JumpsRepository(get_pool())
    session = await repo.get_active_session(interaction.guild_id)
    if not session:
        await interaction.response.send_message(embed=create_info_embed("99k end", "No open session."), ephemeral=True)
        return
    rows = await repo.list_signups(int(session["id"]))
    completed_ids = [int(r["discord_id"]) for r in rows if r.get("status") == "signed_up"]
    ok = await repo.close_session_and_record(session_id=int(session["id"]), guild_id=interaction.guild_id, completed_discord_ids=completed_ids, not_completed_discord_ids=[])
    if not ok:
        await interaction.response.send_message(embed=create_error_embed("Could not close", "Session was already closed."), ephemeral=True)
        return
    await interaction.response.send_message(embed=create_success_embed("99k session ended", f"Closed session #{session['id']}."), ephemeral=True)


bot.tree.add_command(jump99k_group)

@bot.tree.command(name="policy_create", description="Create an insurance policy (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    provider="Provider Discord user",
    policy_name="Policy name",
    description="Policy description",
    cost_type="Premium payment type",
    cost_amount="Premium amount",
    coverage_type="Coverage type",
    payout_description="Payout (items), e.g. xanax=4, edvd=6, ecstasy=1",
    duration_hours="Policy duration in hours"
)
@app_commands.choices(
    cost_type=[
        app_commands.Choice(name="Xanax", value="xanax"),
        app_commands.Choice(name="Erotic DVD", value="erotic_dvd"),
    ],
    coverage_type=[
        app_commands.Choice(name="Xanax", value="xanax"),
        app_commands.Choice(name="Ecstasy After Stack", value="ecstasy_after_stack"),
        app_commands.Choice(name="All Drugs", value="all_drugs"),
    ]
)
async def policy_create(
    interaction: discord.Interaction,
    provider: discord.Member,
    policy_name: str,
    description: str,
    cost_type: app_commands.Choice[str],
    cost_amount: int,
    coverage_type: app_commands.Choice[str],
    payout_description: str,
    duration_hours: int
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        payout_items = parse_payout_string(payout_description)
        if not payout_items:
            raise PayoutParseError("Payout cannot be empty. Example: xanax=4, edvd=6, ecstasy=1")
        request = CreatePolicyRequest(
            guild_id=interaction.guild_id,
            provider_discord_id=provider.id,
            policy_name=policy_name,
            description=description,
            cost_type=cost_type.value,
            cost_amount=cost_amount,
            coverage_type=coverage_type.value,
            payout_description=f"Payout: {payout_items_to_human(payout_items)}",
            payout_items=payout_items,
            duration_hours=duration_hours
        )
        response = await admin_handlers.create_policy_handler(request, provider.id)
        embed = create_success_embed("Policy Created", f"Policy #{response.policy_id} created.")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayoutParseError as e:
        await interaction.followup.send(embed=create_error_embed("Invalid Payout String", f"{e}\nExamples: `xanax=4, edvd=6, ecstasy=1` or `xanax:4,edvd:6`"), ephemeral=True)
    except Exception as e:
        log.exception(f"Policy create failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Policy Create Failed", str(e)), ephemeral=True)


@bot.tree.command(name="provider_approve", description="Approve or reject an insurance provider (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(provider_id="Provider ID", status="Approval status")
@app_commands.choices(
    status=[
        app_commands.Choice(name="Approved", value="approved"),
        app_commands.Choice(name="Rejected", value="rejected"),
        app_commands.Choice(name="Disabled", value="disabled"),
    ]
)
async def provider_approve(
    interaction: discord.Interaction,
    provider_id: int,
    status: app_commands.Choice[str]
):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.approve_provider_handler(
            provider_id,
            status.value,
            interaction.user.id,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Provider Updated", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Provider approval failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Provider Update Failed", str(e)), ephemeral=True)


@bot.tree.command(name="claim_approve", description="Approve an insurance claim (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(claim_id="Claim ID to approve")
async def claim_approve(interaction: discord.Interaction, claim_id: int):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.approve_claim_handler(claim_id, interaction.user.id, source="discord")
        await interaction.followup.send(embed=create_success_embed("Claim Approved", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Claim approve failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Claim Approve Failed", str(e)), ephemeral=True)


@bot.tree.command(name="claim_reject", description="Reject an insurance claim (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(claim_id="Claim ID to reject", notes="Optional rejection notes")
async def claim_reject(interaction: discord.Interaction, claim_id: int, notes: str = None):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        response = await admin_handlers.reject_claim_handler(
            claim_id,
            interaction.user.id,
            notes=notes,
            source="discord"
        )
        await interaction.followup.send(embed=create_success_embed("Claim Rejected", response.message), ephemeral=True)
    except Exception as e:
        log.exception(f"Claim reject failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Claim Reject Failed", str(e)), ephemeral=True)




@bot.tree.command(name="insurers", description="Browse approved insurers in this server")
@app_commands.describe(
    active_only="Show only active insurers and active policies",
    coverage_type="Filter insurer policies by coverage type",
    jump_type="Filter policies by covered jump type (default: 99k)",
)
@app_commands.choices(
    coverage_type=[
        app_commands.Choice(name="Xanax Stack", value="xanax_stack"),
        app_commands.Choice(name="Ecstasy After Stack", value="ecstasy_after_stack"),
        app_commands.Choice(name="All Drugs", value="all_drugs"),
    ]
)
async def insurers(
    interaction: discord.Interaction,
    active_only: bool = True,
    coverage_type: app_commands.Choice[str] = None,
    jump_type: str = "99k",
):
    if not interaction.guild_id:
        await interaction.response.send_message(embed=create_error_embed("Unavailable", "This command only works in a server."), ephemeral=True)
        return

    normalized_jump = (jump_type or "99k").strip()
    normalized_coverage = coverage_type.value if coverage_type else None
    if normalized_coverage == "xanax_stack":
        normalized_coverage = "xanax"

    view = InsurerBrowserView(
        guild_id=interaction.guild_id,
        active_only=active_only,
        coverage_type=normalized_coverage,
        jump_type=normalized_jump,
        timeout=300,
    )
    embed = await view.build_embed(bot)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="request_insurer", description="Submit an insurer approval application")
async def request_insurer(interaction: discord.Interaction):
    await interaction.response.send_modal(RequestInsurerModal())


@bot.tree.command(name="request_host99k", description="Submit a host99k approval application")
async def request_host99k(interaction: discord.Interaction):
    await interaction.response.send_modal(RequestHost99kModal())


@bot.tree.command(name="application_review", description="Review insurer/host99k applications (Admin only)")
@app_commands.describe(
    category="Application category",
    application_id="Application ID",
    decision="Approve or deny",
    reason="Optional reason, especially for denials",
)
@app_commands.choices(
    category=[
        app_commands.Choice(name="insurer", value="insurer"),
        app_commands.Choice(name="host99k", value="host99k"),
    ],
    decision=[
        app_commands.Choice(name="approve", value="approve"),
        app_commands.Choice(name="deny", value="deny"),
    ],
)
async def application_review(
    interaction: discord.Interaction,
    category: app_commands.Choice[str],
    application_id: int,
    decision: app_commands.Choice[str],
    reason: str = None,
):
    await interaction.response.defer(ephemeral=True)
    if not await _can_review_applications(interaction):
        return

    chosen_category = category.value
    chosen_decision = decision.value
    reason_text = reason.strip() if reason else None

    try:
        review = await perform_application_review(
            category=chosen_category,
            application_id=application_id,
            decision="approve" if chosen_decision == "approve" else "deny",
            admin_discord_id=interaction.user.id,
            reason=reason_text,
            guild_id_hint=interaction.guild_id,
        )
        if not review:
            await interaction.followup.send(embed=create_error_embed("Not Found", f"{chosen_category} application `{application_id}` not found."), ephemeral=True)
            return

        applicant_discord_id = review["applicant_discord_id"]
        dm_status = "Applicant DM sent."
        if interaction.guild and applicant_discord_id:
            member = interaction.guild.get_member(int(applicant_discord_id))
            if not member:
                try:
                    member = await interaction.guild.fetch_member(int(applicant_discord_id))
                except Exception:
                    member = None

            if member:
                decision_word = "approved" if chosen_decision == "approve" else "denied"
                dm_embed = create_info_embed(
                    "Application Review Result",
                    f"Your **{chosen_category}** application (ID `{application_id}`) was **{decision_word}**."
                    + (f"\nReason: {reason_text}" if reason_text and chosen_decision == "deny" else ""),
                )
                try:
                    await member.send(embed=dm_embed)
                except discord.Forbidden:
                    dm_status = "Could not DM applicant (DMs disabled)."
            else:
                dm_status = "Could not resolve applicant for DM."

        await interaction.followup.send(
            embed=create_success_embed(
                "Application Reviewed",
                f"{chosen_category} application `{application_id}` set to **{chosen_decision}**. {dm_status}",
            ),
            ephemeral=True,
        )
    except RuntimeError as e:
        await interaction.followup.send(embed=create_error_embed("Unavailable", str(e)), ephemeral=True)
    except Exception as e:
        log.exception("Application review failed: %s", e)
        await interaction.followup.send(embed=create_error_embed("Application Review Failed", str(e)), ephemeral=True)



@bot.tree.command(name="audit_log", description="View recent audit log entries (Admin only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(limit="Number of entries to show (max 20)")
async def audit_log(interaction: discord.Interaction, limit: int = 10):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return
    try:
        db = get_database()
        entries = await db.get_audit_logs(guild_id=interaction.guild_id, limit=min(limit, 20))
        if not entries:
            await interaction.followup.send(embed=create_info_embed("Audit Log", "No audit entries found."), ephemeral=True)
            return
        lines = []
        for entry in entries:
            lines.append(
                f"{entry['action']} • actor {entry.get('actor_discord_id')} • target {entry.get('target_id')}"
            )
        embed = create_info_embed("Audit Log", "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        log.exception(f"Audit log failed: {e}")
        await interaction.followup.send(embed=create_error_embed("Audit Log Failed", str(e)), ephemeral=True)


# ============================================================================
# BACKGROUND WORKERS
# ============================================================================

@tasks.loop(seconds=config.CLEANUP_INTERVAL)
async def cleanup_worker():
    """Background cleanup task.

    This repository has been refactored to the new `jump_99k_*` schema.
    Any legacy cleanup logic referencing `happy_jump_*` tables must never run.

    If you later add time-based automation (reservation expiry, waitlists, etc.)
    for 99k sessions, implement it against `jump_99k_*` tables in
    `repositories/jumps.py` and call it here.
    """
    return


@cleanup_worker.before_loop
async def before_cleanup_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.READINESS_REFRESH_INTERVAL)
async def readiness_worker():
    """Refresh readiness snapshots for the active 99k session in each guild."""
    try:
        db = get_database()
        repo = JumpsRepository(db.pool)
        users_repo = UsersRepository(db.pool)
        torn_api = get_torn_api()
        security = get_security_manager()

        for guild in bot.guilds:
            session = await repo.get_active_session(guild.id)
            if not session:
                continue

            session_id = int(session["id"])
            signups = await repo.list_signups(session_id)

            # Include host in readiness checks as well.
            participant_ids = {int(session.get("host_discord_id"))}
            for s in signups:
                if s.get("status") in {"signed_up", "completed", "not_completed"}:
                    participant_ids.add(int(s["discord_id"]))

            for discord_id in sorted(participant_ids):
                try:
                    key_row = await users_repo.get_user_api_key(discord_id)
                    if not key_row:
                        continue

                    key_data = dict(key_row)
                    # Compatibility: some environments use api_key_encrypted, others encrypted_key.
                    if "encrypted_key" not in key_data and "api_key_encrypted" in key_data:
                        key_data["encrypted_key"] = key_data["api_key_encrypted"]

                    api_key = security.decrypt_api_key(key_data["encrypted_key"])
                    bars = await torn_api.get_user_bars_v2(api_key)
                    cooldowns = await torn_api.get_user_cooldowns_v2(api_key)

                    bars_data = (bars or {}).get("bars") or {}
                    energy_data = bars_data.get("energy") or {}
                    energy = int(energy_data.get("current", 0))
                    energy_max = int(energy_data.get("maximum", 0) or energy_data.get("max", 0) or 0)

                    cd_data = (cooldowns or {}).get("cooldowns") or {}
                    drug_cd = int(cd_data.get("drug", 0))

                    status_text = _get_readiness_status(
                        {"energy": energy, "energy_max": energy_max},
                        drug_cd,
                    )
                    await repo.upsert_readiness_snapshot(
                        session_id=session_id,
                        guild_id=int(session["guild_id"]),
                        discord_id=discord_id,
                        energy=energy,
                        energy_max=energy_max,
                        drug_cooldown=drug_cd,
                        status_text=status_text,
                    )
                except Exception as e:
                    log.warning(f"Failed to refresh 99k readiness for user {discord_id}: {e}")

                await asyncio.sleep(0.35)
    except Exception as e:
        log.error(f"Readiness worker error: {e}", exc_info=True)


@readiness_worker.before_loop
async def before_readiness_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.INSURANCE_CHECK_INTERVAL)
async def insurance_monitor():
    """Monitor insurance coverage and process automatic claims."""
    try:
        db = get_database()
        torn_api = get_torn_api()
        security = get_security_manager()
        
        # Create repository instance
        insurance_repo = InsuranceRepository(db.pool)
        
        # Expire old coverage
        await insurance_repo.expire_coverage()
        
        # Get all active coverage
        active_coverage = await insurance_repo.get_active_coverage()
        
        for coverage in active_coverage:
            try:
                api_key_data = await db.get_user_api_key(coverage['user_discord_id'])
                if not api_key_data:
                    continue
                
                api_key = security.decrypt_api_key(api_key_data['encrypted_key'])
                
                # Get last checked timestamp
                last_check = coverage.get('last_log_timestamp', 0)
                
                # Check logs for drug events
                drug_logs = await torn_api.check_drug_use_logs(api_key, since_timestamp=last_check)
                
                for log_entry in drug_logs:
                    # Check if this is an overdose event
                    od_event = await torn_api.identify_overdose_event(log_entry)
                    
                    if od_event:
                        # Check if claim already exists
                        log_id = od_event.get('log_id') or log_entry.get('id') or log_entry.get('log_id')
                        if log_id:
                            existing = await insurance_repo.check_existing_claim(coverage['coverage_id'], log_id)
                            if existing:
                                continue
                        
                        # Create claim
                        await _create_insurance_claim(coverage, od_event, log_entry)
                
                # Update last check timestamp
                if drug_logs:
                    latest_ts = max(log_entry.get('timestamp', 0) for log_entry in drug_logs)
                    if latest_ts > last_check:
                        await insurance_repo.update_coverage_last_check(coverage['coverage_id'], latest_ts)
                
            except Exception as e:
                log.warning(f"Failed to monitor coverage {coverage['coverage_id']}: {e}")
            
            # Small delay between checks
            await asyncio.sleep(0.5)
    
    except Exception as e:
        log.error(f"Insurance monitor error: {e}", exc_info=True)


@insurance_monitor.before_loop
async def before_insurance_monitor():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.RAFFLE_COMPLETION_INTERVAL)
async def raffle_completion_worker():
    """Check for completed raffles and draw winners."""
    try:
        db = get_database()
        
        # Create repository instance
        raffles_repo = RafflesRepository(db.pool)
        
        # Get raffles that need to be drawn
        raffles_to_draw = await raffles_repo.get_raffles_to_draw()
        
        for raffle in raffles_to_draw:
            try:
                await _draw_raffle_winner(raffle)
            except Exception as e:
                log.error(f"Failed to draw raffle {raffle['raffle_id']}: {e}")
    
    except Exception as e:
        log.error(f"Raffle completion worker error: {e}", exc_info=True)


@raffle_completion_worker.before_loop
async def before_raffle_completion_worker():
    await bot.wait_until_ready()


# ============================================================================
# WORKER HELPERS
# ============================================================================

def _get_readiness_status(bars: dict, drug_cd: int) -> str:
    """Determine readiness status text."""
    energy = bars.get('energy', 0)
    energy_max = bars.get('energy_max', 150)
    
    if drug_cd > 0:
        minutes = drug_cd // 60
        return f"CD: {minutes}m"
    elif energy < config.MIN_ENERGY_REQUIREMENT:
        return f"Low E: {energy}/{energy_max}"
    else:
        return "Ready"


async def _create_insurance_claim(coverage: dict, od_event: dict, raw_log: dict):
    """Create an insurance claim from an overdose event."""
    db = get_database()
    
    try:
        # Create repository instance
        insurance_repo = InsuranceRepository(db.pool)
        
        # Get policy for payout calculation
        policy = await insurance_repo.get_policy(coverage['policy_id'])
        if not policy:
            return
        
        # Determine claim type and payout
        claim_type = od_event.get('type', 'xanax_overdose')
        xanax_lost = coverage.get('xanax_covered', 1)
        payout_amount = coverage.get('payout_amount', 0)
        
        # Create claim
        claim_id = await insurance_repo.create_claim(
            coverage_id=coverage['coverage_id'],
            policy_id=coverage['policy_id'],
            user_discord_id=coverage['user_discord_id'],
            provider_id=policy['provider_id'],
            claim_type=claim_type,
            xanax_lost=xanax_lost,
            payout_amount=payout_amount,
            payout_items=policy.get('payout_items') or [],
            torn_log_id=od_event.get('log_id'),
            torn_log_timestamp=od_event.get('timestamp'),
            torn_log_evidence=json.dumps(raw_log)
        )
        
        log.info(f"Created insurance claim #{claim_id} for coverage #{coverage['coverage_id']}")
        
        # Notify provider
        try:
            provider = await insurance_repo.get_provider_by_id(policy['provider_id'])
            if provider:
                for guild in bot.guilds:
                    member = guild.get_member(provider['discord_id'])
                    if member:
                        claim = await insurance_repo.get_claim(claim_id)
                        embed = create_claim_notification_embed(claim, coverage)
                        try:
                            await member.send(embed=embed)
                        except discord.Forbidden:
                            pass
                        break
        except Exception as e:
            log.warning(f"Failed to notify provider of claim: {e}")
        
    except Exception as e:
        log.error(f"Failed to create insurance claim: {e}")


async def _draw_raffle_winner(raffle: dict):
    """Draw a winner for a completed raffle."""
    db = get_database()
    
    # Create repository instances
    raffles_repo = RafflesRepository(db.pool)
    audit_repo = AuditRepository(db.pool)
    
    raffle_id = raffle['raffle_id']
    
    # Mark as drawing to prevent duplicate draws
    await raffles_repo.update_raffle(raffle_id, status='drawing')
    
    try:
        # Draw winner
        winner = await raffles_repo.draw_raffle_winner(raffle_id)
        
        # Log the draw
        await audit_repo.log_audit(
            None,  # System action (no actor)
            "raffle_auto_drawn",
            "raffle",
            raffle_id,
            {"winner_discord_id": winner['discord_id'] if winner else None},
            guild_id=raffle['guild_id'],
            source='system'
        )
        
        # Get channel to announce winner
        guild = bot.get_guild(raffle['guild_id'])
        if not guild:
            return
        
        settings = await db.get_guild_settings(raffle['guild_id'])
        channel_id = settings.get('raffle_channel_id')
        
        if not channel_id:
            log.warning("Raffle channel not configured for guild %s; skipping raffle completion announcement", raffle['guild_id'])
            return
        
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        if channel is None and channel_id:
            try:
                fetched = await guild.fetch_channel(int(channel_id))
                if hasattr(fetched, "send"):
                    channel = fetched
            except Exception:
                channel = None
        if not channel:
            await db.update_guild_settings(raffle['guild_id'], raffle_channel_id=None)
            log.warning("Configured raffle channel invalid for guild %s; cleared raffle_channel_id", raffle['guild_id'])
            return

        me = guild.me or guild.get_member(getattr(bot.user, 'id', 0))
        if me is None:
            log.warning("Bot member unavailable while completing raffle %s", raffle_id)
            return
        perms = channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.send_messages:
            missing.append("Send Messages")
        if not perms.embed_links:
            missing.append("Embed Links")
        if missing:
            log.warning("Missing raffle completion channel permissions guild=%s channel=%s missing=%s", raffle['guild_id'], channel_id, ', '.join(missing))
            return
        
        # Get updated raffle data
        updated_raffle = await raffles_repo.get_raffle(raffle_id)
        
        if winner:
            # Announce winner
            embed = create_raffle_winner_embed(updated_raffle, winner)
            await channel.send(embed=embed)
            
            # Try to DM winner
            try:
                winner_member = guild.get_member(winner['discord_id'])
                if winner_member:
                    dm_embed = create_success_embed(
                        f"{config.EMOJI_TROPHY} You Won the Raffle!",
                        f"Congratulations! You won the raffle for:\n\n**{raffle['prize']}**\n\n"
                        f"Your winning ticket: #{winner.get('ticket_number', '?')} of {winner.get('total_tickets', '?')}\n\n"
                        f"Please contact the raffle creator to claim your prize."
                    )
                    await winner_member.send(embed=dm_embed)
            except discord.Forbidden:
                pass
        else:
            # No entries - announce no winner
            embed = create_info_embed(
                f"{config.EMOJI_TICKET} Raffle Ended - No Winner",
                f"The raffle for **{raffle['prize']}** has ended with no valid entries."
            )
            await channel.send(embed=embed)
        
        # Update original message if possible
        if raffle.get('announcement_message_id'):
            try:
                message = await channel.fetch_message(raffle['announcement_message_id'])
                entries = await raffles_repo.get_raffle_entries(raffle_id)
                
                if winner:
                    embed = create_raffle_winner_embed(updated_raffle, winner)
                else:
                    embed = create_raffle_embed(updated_raffle, entries)
                
                try:
                    await message.edit(embed=embed, view=None)
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("Raffle completion edit failed guild=%s channel=%s message=%s", raffle.get("guild_id"), getattr(channel, "id", None), raffle.get("announcement_message_id"))
            except discord.NotFound:
                pass

        log.info(f"Raffle #{raffle_id} completed. Winner: {winner['discord_id'] if winner else 'None'}")
        
    except Exception as e:
        log.error(f"Error drawing raffle {raffle_id}: {e}")
        # Reset status if draw failed
        await raffles_repo.update_raffle(raffle_id, status='active')
        raise


async def main():
    """Main entry point for the Discord bot process."""
    config.validate_config()
    log.info("Starting Discord bot service")

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
