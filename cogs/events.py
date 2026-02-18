"""
Happy Jump Discord Bot - Discord-only service
Discord bot process entrypoint (no embedded web server).
"""

import discord
import asyncpg
from discord import app_commands
from discord.ext import commands, tasks
import logging
import asyncio
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

import config
from utils import init_database, get_database, init_torn_api, get_torn_api, init_security, get_security_manager, GuildSettingsRepository, require_api_key
from utils.database import get_pool
from utils.embeds import (
    create_success_embed, create_error_embed, create_warning_embed, create_info_embed,
    create_api_key_guide_embed, create_statistics_embed,
    create_raffle_embed, create_raffle_winner_embed, create_claim_notification_embed
)
from views import (
    ApiKeyIntroView, ConfirmRemoveKeyView, ApplicationReviewView, InsurerBrowserView
)
from views.components import InsuranceOfferView
from views.timezone_picker import TimezonePromptView, send_timezone_picker
from utils.payouts import parse_payout_string, payout_items_to_human, PayoutParseError
from utils.torn_api import TornAPIError, TornAPIPermissionError, TornAPIRateLimitError
from utils.payment_normalization import parse_payment_type
from setup_panel import (
    DEFAULT_WELCOME_TEMPLATE,
    detect_rules_channel,
    has_setup_permission,
    render_welcome_template,
    send_setup_panel,
    InsurerProfileModal,
)
from cogs.pools import register_persistent_pool_views

from bot_actions import handlers as admin_handlers
from bot_actions.application_review import perform_application_review
from services import InsuranceService, DomainError, InvalidInput
from services.overdose_tracker import OverdoseTracker, OverdoseTrackerError
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
from repositories.overdose import OverdoseRepository
from repositories.torn_items import TornItemsRepository, norm_name
from repositories.host_tax import HostTaxRepository
from services.payment_receipts import PaymentReceiptService

log = logging.getLogger("happy_jumper")


HOST_TAX_VERIFY_WINDOW_MINUTES = 30
TORN_NAME_CACHE_TTL_MINUTES = 10
_TORN_NAME_CACHE: dict[int, tuple[str, datetime]] = {}
_TORN_NAME_FAIL_LOG_CACHE: dict[int, datetime] = {}




def _parse_optional_session_start(raw_value: str, _settings: dict, *, host_timezone_name: str | None = None) -> tuple[Optional[datetime], Optional[str], bool]:
    value = str(raw_value or "").strip()
    if not value:
        return None, None, False

    def _to_discord_ts(dt_utc: datetime) -> tuple[datetime, str]:
        unix_ts = int(dt_utc.timestamp())
        return dt_utc, f"<t:{unix_ts}:F>"

    now_utc = datetime.now(timezone.utc)

    # 99k scheduling stores UTC only. Discord <t:...> then renders local viewer time.
    # Without timezone profile settings, plain date/time input is interpreted as UTC.

    discord_ts_match = re.fullmatch(r"<t:(\d{1,17})(?::[tTdDfFR])?>", value)
    if discord_ts_match:
        try:
            unix_ts = int(discord_ts_match.group(1))
            dt_utc, scheduled = _to_discord_ts(datetime.fromtimestamp(unix_ts, tz=timezone.utc))
            return dt_utc, scheduled, False
        except (TypeError, ValueError, OSError):
            raise ValueError("invalid start")

    relative_match = re.fullmatch(r"in\s+((?:\d+\s*[dhm]\s*)+)", value, re.IGNORECASE)
    if relative_match:
        duration = relative_match.group(1)
        parts = re.findall(r"(\d+)\s*([dhm])", duration, flags=re.IGNORECASE)
        if not parts:
            raise ValueError("invalid start")
        delta = timedelta()
        for amount_text, unit in parts:
            amount = int(amount_text)
            normalized = unit.lower()
            if normalized == "d":
                delta += timedelta(days=amount)
            elif normalized == "h":
                delta += timedelta(hours=amount)
            elif normalized == "m":
                delta += timedelta(minutes=amount)
        if delta <= timedelta(0):
            raise ValueError("invalid start")
        dt_utc, scheduled = _to_discord_ts(now_utc + delta)
        return dt_utc, scheduled, False

    iso_candidate = value.replace(" ", "T", 1) if " " in value and "T" not in value else value
    try:
        parsed_iso = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed_iso = None
    if parsed_iso and parsed_iso.tzinfo is not None:
        dt_utc, scheduled = _to_discord_ts(parsed_iso.astimezone(timezone.utc))
        return dt_utc, scheduled, False

    offset_match = re.fullmatch(
        r"\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\s+|T)(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*([+-]\d{2}:?\d{2})\s*",
        value,
    )
    if offset_match:
        year_text, month_text, day_text, hour_text, minute_text, meridiem_text, offset_text = offset_match.groups()
        year = int(year_text)
        month = int(month_text)
        day = int(day_text)
        hour = int(hour_text)
        minute = int(minute_text) if minute_text is not None else 0
        if meridiem_text:
            if hour < 1 or hour > 12:
                raise ValueError("invalid start")
            meridiem = meridiem_text.lower()
            hour = (0 if hour == 12 else hour) if meridiem == "am" else (12 if hour == 12 else hour + 12)
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("invalid start")
        cleaned_offset = f"{offset_text[:3]}:{offset_text[-2:]}" if len(offset_text) == 5 else offset_text
        try:
            parsed_offset = datetime.fromisoformat(
                f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:00{cleaned_offset}"
            )
        except ValueError as exc:
            raise ValueError("invalid start") from exc
        dt_utc, scheduled = _to_discord_ts(parsed_offset.astimezone(timezone.utc))
        return dt_utc, scheduled, False

    match = re.fullmatch(
        r"\s*(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})(?:\s+|T)(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*",
        value,
    )
    if not match:
        raise ValueError("invalid start")

    year_text, month_text, day_text, hour_text, minute_text, meridiem_text = match.groups()
    year = int(year_text) if year_text else now_utc.year
    month = int(month_text)
    day = int(day_text)
    hour = int(hour_text)
    minute = int(minute_text) if minute_text is not None else 0
    if minute < 0 or minute > 59:
        raise ValueError("invalid start")
    if meridiem_text:
        if hour < 1 or hour > 12:
            raise ValueError("invalid start")
        meridiem = meridiem_text.lower()
        hour = (0 if hour == 12 else hour) if meridiem == "am" else (12 if hour == 12 else hour + 12)
    elif hour < 0 or hour > 23:
        raise ValueError("invalid start")

    parse_timezone = timezone.utc
    used_utc_fallback = False
    if host_timezone_name:
        try:
            parse_timezone = ZoneInfo(host_timezone_name)
        except ZoneInfoNotFoundError:
            parse_timezone = timezone.utc
            used_utc_fallback = True
    else:
        used_utc_fallback = True

    try:
        aware_local = datetime(year, month, day, hour, minute, tzinfo=parse_timezone)
    except ValueError as exc:
        raise ValueError("invalid start") from exc

    if not year_text:
        local_now = now_utc.astimezone(parse_timezone)
        if aware_local < (local_now - timedelta(minutes=5)):
            next_year = local_now.year + 1
            while next_year <= local_now.year + 4:
                try:
                    aware_local = aware_local.replace(year=next_year)
                    break
                except ValueError:
                    next_year += 1

    aware_utc = aware_local.astimezone(timezone.utc)
    unix_ts = int(aware_utc.timestamp())
    return aware_utc, f"<t:{unix_ts}:F>", used_utc_fallback


def _format_session_start_display(session: dict) -> str:
    absolute = _format_session_start_ts(session, "F")
    if absolute == "Not set":
        return "Start: Not set"
    return f"Start: {absolute} ({_format_session_start_ts(session, 'R')})"


def _format_session_start_ts(session: dict, style: str = "F") -> str:
    st = session.get("start_time")
    if not st:
        return "Not set"
    if isinstance(st, str):
        try:
            st = datetime.fromisoformat(st)
        except ValueError:
            return "Not set"
    if not isinstance(st, datetime):
        return "Not set"
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    return f"<t:{int(st.timestamp())}:{style}>"

def _host_tax_requirement_text(settings: dict) -> str:
    tax_type = str(settings.get("host_tax_type") or "").strip().lower()
    if tax_type == "cash":
        amount = int(settings.get("host_tax_cash_amount") or 0)
        return f"${amount:,} Torn cash"
    item_id = int(settings.get("host_tax_item_id") or 0)
    qty = int(settings.get("host_tax_quantity") or 0)
    if item_id == 206:
        return f"{qty}x Xanax 💊"
    if item_id == 366:
        return f"{qty}x Erotic DvD 📀"
    return "a configured tax payment"


def _extract_torn_log_id(entry: dict) -> str:
    for key in ("id", "log_id", "log", "logid"):
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return "unknown"


async def try_advisory_lock(pool, lock_key: int) -> bool:
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key))


async def release_advisory_lock(pool, lock_key: int) -> bool:
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT pg_advisory_unlock($1)", lock_key))


async def _fetch_and_upsert_host_readiness_snapshot(
    *,
    repo: JumpsRepository,
    users_repo: UsersRepository,
    session_id: int,
    guild_id: int,
    host_discord_id: int,
) -> dict | None:
    """Fetch host readiness from Torn and upsert snapshot if an API key is available."""
    key_row = await users_repo.get_user_api_key(host_discord_id)
    encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
    if not encrypted_key:
        return None

    try:
        api_key = get_security_manager().decrypt_api_key(encrypted_key)
        user_data = await get_torn_api().get_user_data(api_key)
    except Exception:
        return None

    try:
        energy_current = int((user_data or {}).get("bars", {}).get("energy", {}).get("current", 0) or 0)
        energy_max = int((user_data or {}).get("bars", {}).get("energy", {}).get("maximum", 0) or 0)
        drug_cd = int((user_data or {}).get("cooldowns", {}).get("drug", 0) or 0)
        booster_cd = int((user_data or {}).get("cooldowns", {}).get("booster", 0) or 0)
    except Exception:
        return None

    status_text = "ready" if energy_current >= 1000 and drug_cd == 0 else "not ready"

    try:
        await repo.upsert_readiness_snapshot(
            session_id=session_id,
            guild_id=guild_id,
            discord_id=host_discord_id,
            energy=energy_current,
            energy_max=energy_max,
            drug_cooldown=drug_cd,
            booster_cooldown=booster_cd,
            status_text=status_text,
        )
    except Exception:
        return None
    return {
        "session_id": session_id,
        "guild_id": guild_id,
        "discord_id": host_discord_id,
        "energy": energy_current,
        "energy_max": energy_max,
        "drug_cooldown": drug_cd,
        "booster_cooldown": booster_cd,
        "status_text": status_text,
    }



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
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        embed = create_error_embed("Guild only", "This command can only be used in a server.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    if not settings:
        embed = create_error_embed("Missing configuration", "99k Host settings are not configured for this server.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    member = interaction.user
    has_admin = bool(member.guild_permissions.administrator)

    raw_host_role_id = settings.get("host_role_id")
    if raw_host_role_id in (None, ""):
        raw_host_role_id = settings.get("host99k_role_id")

    host_role_id: int | None = None
    try:
        if raw_host_role_id not in (None, ""):
            host_role_id = int(raw_host_role_id)
    except (TypeError, ValueError):
        host_role_id = None

    has_role = bool(host_role_id) and any(role.id == host_role_id for role in member.roles)
    if has_admin or has_role:
        return True

    embed = create_error_embed("Clearance required.", "You need Administrator or the configured 99k Host role.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return False


class Jump99kSetupModal(discord.ui.Modal, title="99k Setup"):
    host_role_id = discord.ui.TextInput(label="Host role", placeholder="@99k Host", required=True, max_length=20)
    announce_channel_id = discord.ui.TextInput(label="Announce channel", placeholder="#announcements", required=False, max_length=20)
    payee_discord_id = discord.ui.TextInput(label="Payee", placeholder="@HostUser", required=False, max_length=20)
    default_max_slots = discord.ui.TextInput(label="Default max slots (1-7)", placeholder="5", required=False, default="5", max_length=3)

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

        if default_slots < 1 or default_slots > 7:
            await interaction.response.send_message(embed=create_error_embed("Invalid input", "Default max slots must be between 1 and 7."), ephemeral=True)
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


# ============================================================================
# BOT SETUP
# ============================================================================
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.guild_messages = True
intents.dm_messages = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.synced = False
_od_last_checked: dict[tuple[int, int, int], datetime] = {}


async def sync_application_commands() -> None:
    """Sync commands in one scope only (global OR guild), with optional cleanup."""
    if bot.synced:
        log.info("Command sync skipped (already synced).")
        return

    db = get_database()
    lock_key = 82542001
    have_lock = await try_advisory_lock(db.pool, lock_key)
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
        await release_advisory_lock(db.pool, lock_key)


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
    if isinstance(error, discord.HTTPException) and getattr(error, "code", None) == 50035 and "Invalid Form Body" in str(error):
        await _send_interaction_error(
            interaction,
            "This interaction is outdated. Please re-run: /99k edit jump_id:<id> (example: /99k edit jump_id:22).",
        )
        return
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

    insurer_apps = await InsuranceRepository(db.pool).list_pending_insurer_applications()
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

    host_apps = await JumpsRepository(db.pool).list_pending_host_applications()
    for app in host_apps:
        bot.add_view(
            ApplicationReviewView(
                category="host99k",
                application_id=app["id"],
                applicant_discord_id=app["discord_id"],
                guild_id=app["guild_id"],
            )
        )

async def register_persistent_roster_views() -> None:
    """Register persistent roster panel views for active sessions."""
    db = get_database()
    sessions = await JumpsRepository(db.pool).list_active_sessions_with_roster_panel()
    for session in sessions:
        bot.add_view(Jump99kRosterPanelView(int(session["id"])))


async def register_persistent_signup_views() -> None:
    """Register persistent signup views for open session announcement panels."""
    db = get_database()
    sessions = await JumpsRepository(db.pool).list_open_sessions_with_announcement_panels()
    for session in sessions:
        session_id = int(session["id"])
        max_slots = int(session.get("max_slots") or 0)
        is_full = False
        if max_slots > 0:
            signups = await JumpsRepository(db.pool).list_signups(session_id)
            signed_up = sum(1 for row in signups if row.get("status") in {"signed_up", "completed", "not_completed"})
            is_full = signed_up >= max_slots
        bot.add_view(Jump99kSignupView(session_id=session_id, is_full=is_full, is_closed=False))



# ============================================================================
# BOT EVENTS
# ============================================================================

@bot.event
async def on_ready():
    """Bot ready handler."""
    if not bot.intents.message_content:
        log.warning("MessageContent intent is disabled; DM wizard replies may not be readable.")
    log.info(f"Bot logged in as {bot.user}")
    log.info(f"Bot ID: {bot.user.id}")
    log.info(f"Discord.py version: {discord.__version__}")
    log.info(f"Guilds: {len(bot.guilds)}")
    
    await sync_application_commands()
    await register_persistent_application_review_views()
    await register_persistent_roster_views()
    await register_persistent_signup_views()
    await register_persistent_pool_views(bot)
    bot.add_view(TimezonePromptView())

    # Start background workers
    if not cleanup_worker.is_running():
        cleanup_worker.start()
    if not readiness_worker.is_running():
        readiness_worker.start()
    if not overdose_monitor.is_running():
        overdose_monitor.start()
    if not raffle_completion_worker.is_running():
        raffle_completion_worker.start()
    if not auto_verify_99k_payments.is_running():
        auto_verify_99k_payments.start()
    if not roster_panel_refresh_worker.is_running():
        roster_panel_refresh_worker.start()
    
    log.info("✓ Bot is ready!")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Handle bot joining a new guild."""
    log.info(f"Joined guild: {guild.name} ({guild.id})")
    db = get_database()
    repo = GuildSettingsRepository(db)
    try:
        await repo.ensure_guild_exists(guild.id)
        settings = await repo.get_guild_settings(guild.id)
    except Exception:
        log.exception("Failed to initialize guild settings for guild %s", guild.id)
        return

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


@bot.tree.command(name="set_timezone", description="Set your timezone for session time parsing")
async def set_timezone(interaction: discord.Interaction):
    db = get_database()
    users_repo = UsersRepository(db.pool)
    existing = await users_repo.get_user_api_key(interaction.user.id)
    if not existing:
        await interaction.response.send_message(
            "Register your Torn API key first using /set_api_key.",
            ephemeral=True,
        )
        return

    await send_timezone_picker(interaction)


@bot.tree.command(name="remove_api_key", description="Delete your stored Torn API key")
async def remove_api_key(interaction: discord.Interaction):
    """Remove user's stored API key."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    existing = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
    
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
    
    sessions = await JumpsRepository(db.pool).list_open_sessions_by_guild(interaction.guild_id)
    
    user_signups = []
    user_waitlist = []
    hosted_sessions = []
    
    for session in sessions:
        if session['host_discord_id'] == interaction.user.id:
            hosted_sessions.append(session)
        
        signup = await JumpsRepository(db.pool).get_signup(session['id'], interaction.user.id)
        if signup:
            user_signups.append({'session': session, 'signup': signup})
        
        waitlist_pos = None
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
    repo = GuildSettingsRepository(db)
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=create_error_embed("Unavailable", "This command can only be used in a server."),
            ephemeral=True,
        )
        return
    await repo.ensure_guild_exists(interaction.guild.id)
    await send_setup_panel(interaction, db)


@bot.tree.command(name="insurer_profile", description="Edit your insurer profile")
async def insurer_profile(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            embed=create_error_embed("Unavailable", "This command can only be used in a server."),
            ephemeral=True,
        )
        return

    db = get_database()
    settings_repo = GuildSettingsRepository(db)
    settings = await settings_repo.insert_or_get_guild_settings(interaction.guild_id)
    insurer_role_id = settings.get("insurer_role_id")
    if not insurer_role_id:
        await interaction.response.send_message(
            embed=create_error_embed("Insurer role not configured", "Run `/setup` and set the insurer role first."),
            ephemeral=True,
        )
        return

    has_insurer_role = any(role.id == int(insurer_role_id) for role in interaction.user.roles)
    if not has_insurer_role:
        await interaction.response.send_message(
            embed=create_error_embed("Missing role", "You need the configured insurer role to use this command."),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(InsurerProfileModal(db=db))



@bot.tree.command(name="stats", description="View server statistics")
async def stats(interaction: discord.Interaction):
    """Show server statistics."""
    await interaction.response.defer(ephemeral=True)
    db = get_database()
    stats = await JumpsRepository(db.pool).get_guild_statistics(interaction.guild_id)
    
    embed = create_statistics_embed(stats, f"Statistics for {interaction.guild.name}")
    await interaction.followup.send(embed=embed, ephemeral=True)



# ============================================================================
# SLASH COMMANDS - ADMIN ACTIONS
# ============================================================================


@bot.tree.command(name="refresh_api_keys", description="Refresh Torn API key metadata for all users (Admin only)")
@app_commands.default_permissions(administrator=True)
async def refresh_api_keys(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send(
            embed=create_error_embed("Guild Only", "This command can only be used by a guild member."),
            ephemeral=True,
        )
        return

    member = interaction.user
    is_allowed = interaction.guild.owner_id == member.id or member.guild_permissions.administrator
    if not is_allowed:
        await interaction.followup.send(
            embed=create_error_embed("Not Authorized", "Only the guild owner or an Administrator can use this command."),
            ephemeral=True,
        )
        return

    db = get_database()
    users_repo = UsersRepository(db.pool)
    rows = await users_repo.list_all_user_api_keys()

    if not rows:
        await interaction.followup.send(
            embed=create_info_embed("No API Keys", "No stored user API keys were found to refresh."),
            ephemeral=True,
        )
        return

    torn = get_torn_api()
    security = get_security_manager()

    total = len(rows)
    refreshed = 0
    failed = 0
    skipped = 0
    failure_samples: list[str] = []

    progress_message = await interaction.followup.send(
        embed=create_info_embed("Refreshing API Keys", f"Starting refresh for **{total}** stored keys..."),
        ephemeral=True,
    )

    def _record_failure(reason: str) -> None:
        nonlocal failed
        failed += 1
        if len(failure_samples) < 15:
            failure_samples.append(reason[:300])

    for index, row in enumerate(rows, start=1):
        raw_discord_id = row.get("discord_id")
        try:
            discord_id = int(raw_discord_id)
        except (TypeError, ValueError):
            skipped += 1
            if len(failure_samples) < 15:
                failure_samples.append(f"Row {index}: invalid discord_id={raw_discord_id!r}")
            continue

        encrypted = row.get("encrypted_key") or row.get("api_key_encrypted")
        if not encrypted:
            skipped += 1
            if len(failure_samples) < 15:
                failure_samples.append(f"<@{discord_id}>: missing encrypted key")
            continue

        try:
            api_key = security.decrypt_api_key(encrypted)
        except Exception:
            _record_failure(f"<@{discord_id}>: failed to decrypt API key")
            continue

        try:
            data = await torn.get_user_data(api_key)
        except TornAPIRateLimitError as exc:
            _record_failure(f"<@{discord_id}>: Torn API rate limited ({exc})")
            continue
        except TornAPIPermissionError as exc:
            _record_failure(f"<@{discord_id}>: Torn API permission denied ({exc})")
            continue
        except TornAPIError as exc:
            _record_failure(f"<@{discord_id}>: Torn API error ({exc})")
            continue
        except Exception:
            _record_failure(f"<@{discord_id}>: unexpected API failure")
            continue

        try:
            discord_raw = data["discord"]["discord_id"]
            discord_id_api = int(discord_raw)
        except Exception:
            _record_failure(f"<@{discord_id}>: API response missing discord linkage")
            continue

        if discord_id_api != discord_id:
            _record_failure(f"<@{discord_id}>: discord mismatch (api={discord_id_api})")
            continue

        profile = data.get("profile") if isinstance(data, dict) else {}
        try:
            torn_user_id = int(profile["id"])
        except Exception:
            _record_failure(f"<@{discord_id}>: API response missing profile id")
            continue

        torn_name_raw = None
        if isinstance(profile, dict):
            torn_name_raw = profile.get("name") or profile.get("username")
        if torn_name_raw is None and isinstance(data, dict):
            torn_name_raw = data.get("name")
        torn_name = str(torn_name_raw).strip() if torn_name_raw is not None else None
        if torn_name == "":
            torn_name = None

        try:
            await users_repo.update_torn_identity(
                discord_id=discord_id,
                torn_user_id=torn_user_id,
                torn_name=torn_name,
            )
            refreshed += 1
        except Exception:
            _record_failure(f"<@{discord_id}>: database update failed")

        if index % 25 == 0 or index == total:
            await progress_message.edit(
                embed=create_info_embed(
                    "Refreshing API Keys",
                    f"Processed **{index}/{total}**\nRefreshed: **{refreshed}** | Failed: **{failed}** | Skipped: **{skipped}**",
                )
            )

    summary_lines = [
        f"Processed: **{total}**",
        f"Refreshed: **{refreshed}**",
        f"Failed: **{failed}**",
        f"Skipped: **{skipped}**",
    ]
    if failure_samples:
        summary_lines.append("")
        summary_lines.append("**Sample failures:**")
        summary_lines.extend([f"• {sample}" for sample in failure_samples])

    await progress_message.edit(
        embed=create_success_embed("API Key Refresh Complete", "\n".join(summary_lines))
    )


@bot.tree.command(name="refresh_item_icons", description="Refresh Torn item icon index (Admin only)")
@app_commands.default_permissions(administrator=True)
async def refresh_item_icons(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not await ensure_admin(interaction):
        return

    db = get_database()
    if not await require_api_key(interaction, db, "refresh item icons"):
        return
    row = await UsersRepository(db.pool).get_user_api_key(interaction.user.id)
    encrypted = row.get("encrypted_key") or row.get("api_key_encrypted")

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



async def _disable_99k_session_messages(bot_client: commands.Bot, session: dict, *, status_text: str) -> None:
    repo = JumpsRepository(get_pool())
    await upsert_99k_announcement(
        bot=bot_client,
        repo=repo,
        guild_id=int(session["guild_id"]),
        session_id=int(session["id"]),
        channel_id=int(session["announce_channel_id"]) if session.get("announce_channel_id") else None,
    )

    private_channel_id = session.get("private_channel_id")
    roster_message_id = session.get("roster_message_id")
    if private_channel_id and roster_message_id:
        guild = bot_client.get_guild(int(session["guild_id"]))
        if guild:
            try:
                pch = guild.get_channel(int(private_channel_id)) or await guild.fetch_channel(int(private_channel_id))
                roster_msg = await pch.fetch_message(int(roster_message_id))
                view = discord.ui.View.from_message(roster_msg, timeout=None)
                for child in view.children:
                    child.disabled = True
                await roster_msg.edit(view=view)
            except Exception:
                pass


def _format_99k_price_item_plain(price_item: str | None) -> str:
    normalized = str(price_item or "").strip().lower()
    if normalized == "xanax":
        return "Xanax"
    if normalized in {"erotic dvd", "erotic_dvd", "edvd"}:
        return "Erotic DvD"
    return str(price_item or "Unknown")


def _format_99k_price_item_label(price_item: str | None) -> str:
    base = _format_99k_price_item_plain(price_item)
    normalized = str(price_item or "").strip().lower()
    if normalized == "xanax":
        return f"{base} 💊"
    if normalized in {"erotic dvd", "erotic_dvd", "edvd"}:
        return f"{base} 📀"
    return base


def _priority_status_text(session: dict) -> str:
    if not bool(session.get("priority_enabled")):
        return "Priority spot: Not offered"

    if session.get("priority_taken_signup_id"):
        return "Priority spot: Taken"

    reserved_until = session.get("priority_reserved_until")
    if reserved_until and reserved_until >= datetime.now(timezone.utc):
        return "Priority spot: Reserved"

    item_label = _format_99k_price_item_plain(session.get("price_item"))
    return f"Priority spot: Available (+1 {item_label})"


def _is_99k_closed(status: str | None) -> bool:
    return str(status or "").strip().lower() in {"closed", "cancelled", "finished", "completed", "expired"}


def _slugify_discord_channel_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "jump"


def _build_99k_private_channel_name(host_torn_name: str, session_id: int, existing_names: set[str]) -> str:
    base_slug = _slugify_discord_channel_name(host_torn_name)
    owner_slug = base_slug if base_slug.endswith("s") else f"{base_slug}s"
    desired_name = f"{owner_slug}-99k-jump"
    if desired_name not in existing_names:
        return desired_name

    with_session_id = f"{desired_name}-{int(session_id)}"
    if with_session_id not in existing_names:
        return with_session_id

    suffix = 2
    while True:
        candidate = f"{with_session_id}-{suffix}"
        if candidate not in existing_names:
            return candidate
        suffix += 1


async def _resolve_99k_host_label(users_repo: UsersRepository, host_discord_id: int) -> str:
    host_row = await users_repo.get_user_api_key(host_discord_id)
    host_torn_id = int((host_row or {}).get("torn_user_id") or 0)
    host_torn_name = str((host_row or {}).get("torn_name") or "").strip() or "User"
    if host_torn_id > 0:
        return f"{host_torn_name} [{host_torn_id}]"
    return f"{host_torn_name} [ID not linked]"


def build_99k_announcement_content(session: dict, signed_up: int, paid: int, host_label: str) -> str:
    session_id = int(session["id"])
    start_text = _format_session_start_display(session)
    price_amount = int(session.get("price_amount") or 0)
    price_item_label = _format_99k_price_item_label(session.get("price_item"))
    notes_or_placeholder = str(session.get("notes") or "None")
    max_slots = int(session.get("max_slots") or 0)
    is_closed = _is_99k_closed(session.get("status"))
    is_full = not is_closed and max_slots > 0 and signed_up >= max_slots
    status_text = "Closed" if is_closed else ("Full" if is_full else "Open")
    priority_text = _priority_status_text(session)
    return (
        f"📣✨ **99k Happy Jump** ✨ — **Session #{session_id}**\n"
        f"👤 Host: {host_label}\n"
        f"🕒 {start_text}\n"
        f"💰 Spot price: {price_amount}x {price_item_label}\n"
        f"⭐ {priority_text}\n"
        f"📝 Notes: {notes_or_placeholder}\n"
        f"👥 Signed up: {signed_up}/{max_slots} • ✅ Paid: {paid}\n"
        f"🔒 Status: {status_text}\n"
        "_Click **Join** to reserve your spot._"
    )


def build_99k_jump_created_announcement_content(session: dict, settings: dict | None = None) -> str:
    start_text = _format_session_start_display(session)
    max_slots = int(session.get("max_slots") or 0)
    price_amount = int(session.get("price_amount") or 0)
    price_item_label = _format_99k_price_item_label(session.get("price_item"))
    priority_text = _priority_status_text(session)
    settings = settings or {}
    jump_channel_id = int(settings.get("jump_99k_channel_id") or 0)
    if jump_channel_id > 0:
        purchase_line = f"To purchase a spot, go to <#{jump_channel_id}> and use the signup message/buttons."
    else:
        purchase_line = "To purchase a spot, use the jump signup channel configured in /setup."
    return (
        f"🔔 There will be a jump.\n"
        f"{start_text}.\n"
        f"{purchase_line}\n"
        f"Please secure your spot with {price_amount}x {price_item_label}.\n"
        f"{priority_text}.\n"
        f"{max_slots}/{max_slots} available spots"
    )


async def post_99k_jump_created_announcement(
    bot: commands.Bot,
    guild_id: int,
    session: dict,
    settings: dict,
) -> None:
    channel_id = int(settings.get("jump_announce_channel_id") or 0)
    if channel_id <= 0:
        return

    guild = bot.get_guild(int(guild_id))
    channel = guild.get_channel(channel_id) if guild else bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await (guild.fetch_channel(channel_id) if guild else bot.fetch_channel(channel_id))
        except Exception:
            log.exception(
                "99k jump announcement channel fetch failed guild_id=%s channel_id=%s session_id=%s",
                guild_id,
                channel_id,
                session.get("id"),
            )
            return

    try:
        role_ids = GuildSettingsRepository._normalize_role_id_list(settings.get("jump_ping_role_ids"))
        role_mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
        content = build_99k_jump_created_announcement_content(session, settings)
        prefix = f"{role_mentions}\n" if role_mentions else ""
        await channel.send(f"{prefix}{content}")
    except Exception:
        log.exception(
            "99k jump announcement post failed guild_id=%s channel_id=%s session_id=%s",
            guild_id,
            getattr(channel, "id", channel_id),
            session.get("id"),
        )


async def upsert_99k_announcement(
    bot: commands.Bot,
    repo: JumpsRepository,
    guild_id: int,
    session_id: int,
    channel_id: int | None,
) -> None:
    session = await repo.get_session(session_id)
    if not session or int(session.get("guild_id", 0)) != int(guild_id):
        return
    users_repo = UsersRepository(get_pool())

    signups = await repo.list_signups(session_id)
    signed_up = sum(1 for row in signups if row.get("status") in {"signed_up", "completed", "not_completed"})
    paid = sum(1 for row in signups if row.get("payment_verified"))
    max_slots = int(session.get("max_slots") or 0)
    is_closed = _is_99k_closed(session.get("status"))
    is_full = not is_closed and max_slots > 0 and signed_up >= max_slots

    target_channel_id = int(channel_id or session.get("announce_channel_id") or 0)
    if target_channel_id <= 0:
        return

    guild = bot.get_guild(int(guild_id))
    channel = guild.get_channel(target_channel_id) if guild else bot.get_channel(target_channel_id)
    if channel is None:
        try:
            channel = await (guild.fetch_channel(target_channel_id) if guild else bot.fetch_channel(target_channel_id))
        except Exception:
            return

    host_discord_id = int(session.get("host_discord_id") or 0)
    host_label = await _resolve_99k_host_label(users_repo, host_discord_id)
    content = build_99k_announcement_content(session, signed_up, paid, host_label)
    view = Jump99kSignupView(session_id=session_id, is_full=is_full, is_closed=is_closed)

    announce_channel_id = session.get("announce_channel_id")
    announce_message_id = session.get("announce_message_id")
    if announce_channel_id and announce_message_id:
        try:
            msg = await channel.fetch_message(int(announce_message_id))
            await msg.edit(content=content, view=view)
            if int(announce_channel_id) != int(channel.id):
                await repo.set_announcement_message(session_id, channel_id=int(channel.id), message_id=int(msg.id))
            return
        except discord.NotFound:
            pass
        except Exception:
            return

    msg = await channel.send(content, view=view)
    await repo.set_announcement_message(session_id, channel_id=int(channel.id), message_id=int(msg.id))

async def _refresh_99k_panel(bot_client: commands.Bot, session_id: int) -> None:
    db = get_database()
    repo = JumpsRepository(db.pool)
    try:
        session = await repo.get_session(session_id)
        if not session:
            return
        await upsert_99k_announcement(
            bot=bot_client,
            repo=repo,
            guild_id=int(session["guild_id"]),
            session_id=int(session_id),
            channel_id=int(session["announce_channel_id"]) if session.get("announce_channel_id") else None,
        )
    except Exception:
        pass


def _format_cd_hhmm(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}"




def _format_energy_pair(current: int | None, maximum: int | None) -> str:
    if current is None or maximum is None:
        return "|?/?|"
    return f"|{int(current)}/{int(maximum)}|"


def _should_log_torn_name_failure(discord_id: int) -> bool:
    now = datetime.now(timezone.utc)
    cached = _TORN_NAME_FAIL_LOG_CACHE.get(discord_id)
    if cached and cached > now:
        return False
    _TORN_NAME_FAIL_LOG_CACHE[discord_id] = now + timedelta(minutes=TORN_NAME_CACHE_TTL_MINUTES)
    return True


async def _get_torn_display_name_for_discord(guild_id: int, discord_id: int) -> Optional[str]:
    now = datetime.now(timezone.utc)
    cached = _TORN_NAME_CACHE.get(discord_id)
    if cached and cached[1] > now:
        return cached[0]

    user_row = await UsersRepository(get_pool()).get_user_api_key(discord_id)
    encrypted_key = (user_row or {}).get("encrypted_key") or (user_row or {}).get("api_key_encrypted")
    if not encrypted_key:
        if _should_log_torn_name_failure(discord_id):
            log.debug("Torn name resolve skipped: no API key guild_id=%s discord_id=%s", guild_id, discord_id)
        return None

    try:
        api_key = get_security_manager().decrypt_api_key(encrypted_key)
        data = await get_torn_api().get_user_data(api_key)
        name = (data.get("profile", {}) or {}).get("name") or data.get("name")
        if name:
            _TORN_NAME_CACHE[discord_id] = (str(name), now + timedelta(minutes=TORN_NAME_CACHE_TTL_MINUTES))
            return str(name)

        if _should_log_torn_name_failure(discord_id):
            log.debug("Torn name resolve returned empty name guild_id=%s discord_id=%s", guild_id, discord_id)
        return None
    except (TornAPIError, TornAPIRateLimitError):
        if _should_log_torn_name_failure(discord_id):
            log.debug("Torn name resolve failed via Torn API guild_id=%s discord_id=%s", guild_id, discord_id)
        return None
    except Exception:
        if _should_log_torn_name_failure(discord_id):
            log.debug("Torn name resolve unexpected failure guild_id=%s discord_id=%s", guild_id, discord_id)
        return None


def _truncate_name_16(name: str) -> str:
    raw = (name or "").strip() or "User"
    return raw if len(raw) <= 14 else f"{raw[:13]}…"


async def _resolve_roster_name(guild: discord.Guild | None, discord_id: int) -> str:
    guild_id = int(guild.id) if guild else 0
    torn_name = await _get_torn_display_name_for_discord(guild_id, discord_id)
    if torn_name:
        return _truncate_name_16(torn_name)

    if guild:
        member = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except Exception:
                member = None
        if member is not None:
            return _truncate_name_16(member.display_name)
    return _truncate_name_16(f"<@{int(discord_id)}>")


def _build_roster_embed(lines: list[str]) -> discord.Embed:
    return discord.Embed(title="Jump Roster", description="\n".join(lines), color=discord.Color.blurple())


async def build_roster_panel(session_id: int, channel: discord.abc.Messageable) -> tuple[discord.Embed, discord.ui.View]:
    repo = JumpsRepository(get_pool())
    session = await repo.get_session(session_id)
    if not session:
        raise ValueError("Session not found")

    signups = await repo.list_roster_signups_with_readiness(session_id)
    progress = await repo.get_jump_progress(session_id)
    readiness_rows = await repo.list_readiness(session_id)
    host_id = int(session["host_discord_id"])
    host_readiness = next((r for r in readiness_rows if int(r.get("discord_id") or 0) == host_id), None)
    if host_readiness is None:
        host_readiness = await _fetch_and_upsert_host_readiness_snapshot(
            repo=repo,
            users_repo=UsersRepository(get_pool()),
            session_id=int(session_id),
            guild_id=int(session["guild_id"]),
            host_discord_id=host_id,
        )

    guild = channel.guild if isinstance(channel, discord.abc.GuildChannel) else None

    async def _display_name_for(discord_id: int) -> str:
        torn = await _get_torn_display_name_for_discord(int(session["guild_id"]), int(discord_id))
        if torn:
            return torn
        member = guild.get_member(int(discord_id)) if guild else None
        if member and getattr(member, "display_name", None):
            return member.display_name
        return f"<@{int(discord_id)}>"

    def _state_label(state: str) -> str:
        if state == "in_progress":
            return "🟦 JUMPING"
        if state == "done":
            return "✅ DONE"
        return "⏳ WAITING"

    host_name = await _display_name_for(host_id)
    host_energy = (host_readiness or {}).get("energy")
    host_energy_max = (host_readiness or {}).get("energy_max")
    host_drug_cd = (host_readiness or {}).get("drug_cooldown") if host_readiness else None
    host_booster_cd = (host_readiness or {}).get("booster_cooldown") if host_readiness else None
    host_ready = host_energy is not None and host_energy_max is not None and int(host_energy) >= 1000 and int(host_drug_cd or 0) == 0
    host_emoji = "🟩" if host_ready else "🟥"

    participants = [row for row in signups if int(row.get("discord_id") or 0) != host_id]
    progress_signups = progress.get("signups") or []
    participant_states: list[str] = []
    for idx, row in enumerate(participants):
        state = "waiting"
        if idx < len(progress_signups):
            state = str(progress_signups[idx].get("state") or "waiting")
        participant_states.append(state)

    host_state = str((progress.get("host") or {}).get("state") or "waiting")
    roster_states = [host_state, *participant_states]
    roster_names: list[str] = [host_name]

    start_display = _format_session_start_ts(session, "F")
    lines = [f"Start: {start_display}"]

    in_progress_index = next((i for i, state in enumerate(roster_states, start=1) if state == "in_progress"), None)
    if in_progress_index is not None:
        lines.extend([f"Now jumping: {roster_names[0] if in_progress_index == 1 else ''}", ""])
    else:
        lines.append("")

    lines.append(
        f"1) Name:{host_name} E-lvl {_format_energy_pair(host_energy, host_energy_max)} Dcd |{_format_cd_hhmm(host_drug_cd)}| Bcd |{_format_cd_hhmm(host_booster_cd)}| {host_emoji} • {_state_label(host_state)}"
    )

    for idx, row in enumerate(participants, start=2):
        discord_id = int(row.get("discord_id") or 0)
        name = await _display_name_for(discord_id)
        roster_names.append(name)
        priority_label = " [Priority jump]" if bool(row.get("is_priority")) else ""

        has_readiness = row.get("checked_at") is not None
        energy = int(row.get("energy") or 0) if has_readiness else None
        energy_max = int(row.get("energy_max") or 0) if has_readiness else None
        drug_cd = row.get("drug_cooldown") if has_readiness else None
        booster_cd = row.get("booster_cooldown") if has_readiness else None

        if bool(row.get("overdose_flag")):
            emoji = "🟧"
        elif energy is not None and energy >= 1000 and int(drug_cd or 0) == 0:
            emoji = "🟩"
        else:
            emoji = "🟥"

        state = participant_states[idx - 2] if idx - 2 < len(participant_states) else "waiting"
        lines.append(
            f"{idx}) Name:{name}{priority_label} E-lvl {_format_energy_pair(energy, energy_max)} Dcd |{_format_cd_hhmm(drug_cd)}| Bcd |{_format_cd_hhmm(booster_cd)}| {emoji} • {_state_label(state)}"
        )

    if in_progress_index is not None and in_progress_index <= len(roster_names):
        lines[1] = f"Now jumping: {roster_names[in_progress_index - 1]}"

    roster_size = min(8, 1 + len(participants))
    enabled_start_positions: set[int] = set()
    enabled_end_positions: set[int] = set()
    if in_progress_index is not None and in_progress_index <= roster_size:
        enabled_end_positions.add(in_progress_index)
    else:
        for idx, state in enumerate(roster_states[:roster_size], start=1):
            if state == "waiting":
                enabled_start_positions.add(idx)
                break

    embed = _build_roster_embed(lines)
    view = Jump99kRosterPanelView(
        session_id,
        roster_size=roster_size,
        enabled_start_positions=enabled_start_positions,
        enabled_end_positions=enabled_end_positions,
    )
    return embed, view


async def _refresh_roster_panel(session_id: int, channel: discord.abc.Messageable, message: discord.Message | None = None) -> tuple[discord.Embed, str]:
    embed, view = await build_roster_panel(session_id, channel)
    roster_text = embed.description or ""

    if message is not None:
        await message.edit(embed=embed, view=view)

    repo = JumpsRepository(get_pool())
    await repo.touch_roster_refreshed(session_id)
    return embed, roster_text


async def _refresh_roster_if_exists(bot_client: commands.Bot, session_id: int) -> None:
    repo = JumpsRepository(get_pool())
    session = await repo.get_session(session_id)
    if not session:
        return

    roster_channel_id = session.get("roster_channel_id") or session.get("private_channel_id")
    roster_message_id = session.get("roster_message_id")
    if not roster_channel_id or not roster_message_id:
        return

    guild = bot_client.get_guild(int(session["guild_id"]))

    try:
        channel = bot_client.get_channel(int(roster_channel_id))
        if channel is None:
            if guild:
                channel = guild.get_channel(int(roster_channel_id))
            if channel is None:
                channel = await bot_client.fetch_channel(int(roster_channel_id))
        roster_message = await channel.fetch_message(int(roster_message_id))
        await _refresh_roster_panel(int(session_id), channel, roster_message)
    except (discord.NotFound, discord.Forbidden):
        await repo.clear_roster_panel_message(int(session_id))
    except discord.HTTPException:
        log.warning("Roster refresh HTTPException for session=%s", session_id)
    except Exception:
        log.exception("Failed to refresh roster panel for session=%s", session_id)


class Jump99kRosterPanelView(discord.ui.View):
    MAX_POSITIONS = 8

    def __init__(
        self,
        session_id: int,
        *,
        roster_size: int | None = None,
        enabled_start_positions: set[int] | None = None,
        enabled_end_positions: set[int] | None = None,
    ):
        super().__init__(timeout=None)
        self.session_id = int(session_id)
        self.roster_size = int(roster_size) if roster_size is not None else self.MAX_POSITIONS
        self.enabled_start_positions = set(enabled_start_positions or set())
        self.enabled_end_positions = set(enabled_end_positions or set())

        refresh_btn = discord.ui.Button(
            label="Refresh roster",
            style=discord.ButtonStyle.primary,
            custom_id=f"99k_roster_refresh:{self.session_id}",
            row=0,
        )
        view_btn = discord.ui.Button(
            label="View roster",
            style=discord.ButtonStyle.secondary,
            custom_id=f"99k_roster_view:{self.session_id}",
            row=0,
        )
        refresh_btn.callback = self._on_refresh
        view_btn.callback = self._on_view
        self.add_item(refresh_btn)
        self.add_item(view_btn)

        for position in range(1, self.MAX_POSITIONS + 1):
            row = 1 + ((position - 1) // 2)
            start_btn = discord.ui.Button(
                label=f"Start {position}",
                style=discord.ButtonStyle.success,
                custom_id=f"99k_roster_start:{self.session_id}:{position}",
                row=row,
                disabled=not self._is_start_enabled(position),
            )
            end_btn = discord.ui.Button(
                label=f"End {position}",
                style=discord.ButtonStyle.danger,
                custom_id=f"99k_roster_end:{self.session_id}:{position}",
                row=row,
                disabled=not self._is_end_enabled(position),
            )
            start_btn.callback = self._build_transition_handler("start", position)
            end_btn.callback = self._build_transition_handler("end", position)
            self.add_item(start_btn)
            self.add_item(end_btn)

    def _is_start_enabled(self, position: int) -> bool:
        if position > self.roster_size:
            return False
        return position in self.enabled_start_positions

    def _is_end_enabled(self, position: int) -> bool:
        if position > self.roster_size:
            return False
        return position in self.enabled_end_positions

    async def _is_authorized(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild_id or not isinstance(interaction.user, discord.Member):
            return False

        member = interaction.user
        if member.guild_permissions.administrator:
            return True

        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if session and int(session.get("host_discord_id") or 0) == int(member.id):
            return True

        settings = await GuildSettingsRepository(get_database()).get_guild_settings(int(interaction.guild_id))
        host_role_id = int(settings.get("host99k_role_id") or 0)
        if host_role_id > 0 and any(int(role.id) == host_role_id for role in member.roles):
            return True

        return False

    def _build_transition_handler(self, action: str, position: int):
        async def _handler(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not await self._is_authorized(interaction):
                await interaction.followup.send("You are not allowed to control this roster.", ephemeral=True)
                return

            repo = JumpsRepository(get_pool())
            ok, message = await repo.run_jump_transition_by_position(
                session_id=self.session_id,
                position=position,
                action=action,
            )
            if not ok:
                await interaction.followup.send(message, ephemeral=True)
                return

            if interaction.channel and interaction.message:
                try:
                    await _refresh_roster_panel(self.session_id, interaction.channel, interaction.message)
                except Exception:
                    log.exception("Failed to refresh roster after transition session_id=%s", self.session_id)
                    await interaction.followup.send("State changed, but panel refresh failed.", ephemeral=True)
                    return

            await interaction.followup.send(message, ephemeral=True)

        return _handler

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.channel:
                await interaction.followup.send("Channel not found.", ephemeral=True)
                return
            embed, roster_text = await _refresh_roster_panel(self.session_id, interaction.channel, interaction.message)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "Roster message edit failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            try:
                _, roster_text = await _refresh_roster_panel(self.session_id, interaction.channel, None)
                await interaction.followup.send(f"Roster message could not be updated, but here is the latest roster:\n{roster_text}", ephemeral=True)
            except Exception:
                await interaction.followup.send("Could not refresh roster right now. Please try again shortly.", ephemeral=True)
        except Exception:
            log.exception(
                "99k refresh_roster failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send("Sorry—refreshing the roster failed. Please try again.", ephemeral=True)

    async def _on_view(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if not interaction.channel:
                await interaction.followup.send("Channel not found.", ephemeral=True)
                return
            _, roster_text = await _refresh_roster_panel(self.session_id, interaction.channel, interaction.message)
            await interaction.followup.send(roster_text, ephemeral=True)
        except (discord.Forbidden, discord.NotFound):
            log.warning(
                "Roster message edit failed during view session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            try:
                _, roster_text = await _refresh_roster_panel(self.session_id, interaction.channel, None)
                await interaction.followup.send(roster_text, ephemeral=True)
            except Exception:
                await interaction.followup.send("Could not fetch the roster right now. Please try again.", ephemeral=True)
        except Exception:
            log.exception(
                "99k view_roster failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send("Sorry—loading the roster failed. Please try again.", ephemeral=True)


class Jump99kUserControlsView(discord.ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=900)
        self.session_id = session_id

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            db = get_database()
            repo = JumpsRepository(db.pool)
            ok = await repo.cancel_signup(session_id=self.session_id, discord_id=interaction.user.id)
            await _refresh_99k_panel(interaction.client, self.session_id)
            await _refresh_roster_if_exists(interaction.client, self.session_id)
            if ok:
                await interaction.followup.send("You’ve been removed.", ephemeral=True)
            else:
                await interaction.followup.send("You weren’t signed up.", ephemeral=True)
        except Exception:
            log.exception(
                "99k leave failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send("Sorry—could not process that action. Please try again.", ephemeral=True)

    @discord.ui.button(label="✅ Verify Payment", style=discord.ButtonStyle.success)
    async def verify_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            db = get_database()
            repo = JumpsRepository(db.pool)
            users_repo = UsersRepository(db.pool)
            security = get_security_manager()
            torn_api = get_torn_api()

            session = await repo.get_session(self.session_id)
            if not session or str(session.get("status", "")).lower() != "open":
                try:
                    if interaction.message:
                        view = discord.ui.View.from_message(interaction.message, timeout=None)
                        for child in view.children:
                            child.disabled = True
                        await interaction.message.edit(view=view)
                except Exception:
                    pass
                await interaction.followup.send("This is closed.", ephemeral=True)
                return
            if int(session.get("guild_id", 0)) != int(interaction.guild_id):
                await interaction.followup.send("Session not found.", ephemeral=True)
                return

            bl = await repo.is_blacklisted(interaction.guild_id, interaction.user.id)
            if bl:
                await interaction.followup.send("You are blacklisted.", ephemeral=True)
                return

            if not await require_api_key(interaction, db, "verify your payment"):
                return
            key_row = await users_repo.get_user_api_key(interaction.user.id)
            encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")

            host_key = await users_repo.get_user_api_key(int(session["host_discord_id"]))
            host_torn_id = int(host_key["torn_user_id"]) if host_key and host_key.get("torn_user_id") else 0
            if not host_torn_id:
                await interaction.followup.send("Host has not linked Torn ID.", ephemeral=True)
                return

            api_key = security.decrypt_api_key(encrypted_key)
            since_ts = int((session["created_at"] - timedelta(seconds=60)).timestamp())
            base_amount = int(session.get("price_amount") or 0)
            priority_increment = int(session.get("priority_increment") or 1)
            priority_amount = base_amount + priority_increment

            item = str(session.get("price_item", "")).lower()
            if item == "xanax":
                payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                paid_amount = priority_amount if payment else base_amount
                if not payment:
                    payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
            elif item == "erotic_dvd":
                payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                paid_amount = priority_amount if payment else base_amount
                if not payment:
                    payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
            else:
                await interaction.followup.send("Unsupported payment item for this session.", ephemeral=True)
                return

            if not payment:
                await interaction.followup.send("Payment not found yet…", ephemeral=True)
                return

            signup = await repo.get_signup(self.session_id, interaction.user.id)
            if not signup:
                await interaction.followup.send("Signup not found.", ephemeral=True)
                return

            if paid_amount == priority_amount:
                await repo.finalize_priority(
                    session_id=self.session_id,
                    buyer_discord_id=interaction.user.id,
                    signup_id=int(signup["id"]),
                )

            await repo.mark_signup_payment_verified(session_id=self.session_id, discord_id=interaction.user.id)
            payer_torn = int(key_row.get("torn_user_id") or 0) or None
            receipts = PaymentReceiptService(db.pool)
            await receipts.create_and_verify(
            featureType="jump_99k",
            featureRefId=self.session_id,
            payer_discord_id=interaction.user.id,
            payer_torn_id=payer_torn,
            payee_discord_id=int(session["host_discord_id"]) or None,
            payee_torn_id=host_torn_id,
            amount=paid_amount,
            currency_type=str(session["price_item"]),
            metadata=payment,
            verifier_discord_id=interaction.user.id,
            verifier_torn_id=payer_torn,
        )
            if interaction.guild:
                await _grant_private_channel_access(interaction.guild, session, interaction.user.id)
            await _refresh_99k_panel(interaction.client, self.session_id)
            await _refresh_roster_if_exists(interaction.client, self.session_id)
            await interaction.followup.send(
                "✅ Payment verified for this 99k session.",
                view=InsuranceOfferView(self.session_id, interaction.user.id),
                ephemeral=True,
            )
        except TornAPIError:
            await interaction.followup.send("Torn API may be down right now. Please try again in a minute.", ephemeral=True)
        except Exception:
            log.exception(
                "99k verify_payment failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send("Sorry—payment verification failed. Please try again.", ephemeral=True)


class Jump99kSignupView(discord.ui.View):
    def __init__(self, session_id: int, is_full: bool, is_closed: bool):
        super().__init__(timeout=None)
        self.session_id = session_id
        if is_closed:
            button = discord.ui.Button(label="Closed", style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"jump99k:join:{session_id}")
        elif is_full:
            button = discord.ui.Button(label="Full", style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"jump99k:join:{session_id}")
        else:
            button = discord.ui.Button(label="Join", style=discord.ButtonStyle.success, disabled=False, custom_id=f"jump99k:join:{session_id}")
        button.callback = self.join
        self.add_item(button)

    async def join(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild_id:
            await interaction.followup.send("Guild context is required.", ephemeral=True)
            return

        try:
            db = get_database()
            repo = JumpsRepository(db.pool)
            users_repo = UsersRepository(db.pool)

            session = await repo.get_session(self.session_id)
            if not session or _is_99k_closed(session.get("status")):
                await _refresh_99k_panel(interaction.client, self.session_id)
                await interaction.followup.send("This jump is closed.", ephemeral=True)
                return
            if int(session.get("guild_id", 0)) != int(interaction.guild_id):
                await interaction.followup.send("Session not found.", ephemeral=True)
                return

            signups = await repo.list_signups(self.session_id)
            signed_up = sum(1 for row in signups if row.get("status") in {"signed_up", "completed", "not_completed"})
            max_slots = int(session.get("max_slots") or 0)
            if max_slots > 0 and signed_up >= max_slots:
                await _refresh_99k_panel(interaction.client, self.session_id)
                await interaction.followup.send("This jump is full.", ephemeral=True)
                return

            bl = await repo.is_blacklisted(interaction.guild_id, interaction.user.id)
            if bl:
                await interaction.followup.send("You are blacklisted.", ephemeral=True)
                return

            if not await require_api_key(interaction, db, "join a 99k jump"):
                return
            key_row = await users_repo.get_user_api_key(interaction.user.id)

            settings = await GuildSettingsRepository(db).get_or_create(interaction.guild_id)
            timeout_minutes = int(settings.get("reservation_timeout_minutes") or config.DEFAULT_RESERVATION_TIMEOUT)
            reserved_until = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)

            torn_user_id = int(key_row["torn_user_id"]) if key_row.get("torn_user_id") else None
            await repo.create_or_restore_signup(
                session_id=self.session_id,
                guild_id=interaction.guild_id,
                discord_id=interaction.user.id,
                torn_user_id=torn_user_id,
                reserved_until=reserved_until,
            )
            await _refresh_99k_panel(interaction.client, self.session_id)

            buyer_signup = await repo.get_signup(self.session_id, interaction.user.id)
            if not buyer_signup:
                await interaction.followup.send("Could not load your signup. Please try again.", ephemeral=True)
                return

            now_utc = datetime.now(timezone.utc)
            priority_reserved_until = session.get("priority_reserved_until")
            priority_offerable = (
                bool(session.get("priority_enabled"))
                and session.get("priority_taken_signup_id") is None
                and (priority_reserved_until is None or priority_reserved_until < now_utc)
            )

            if priority_offerable:
                base_price = int(session.get("price_amount") or 0)
                priority_increment = int(session.get("priority_increment") or 1)
                priority_price = base_price + priority_increment
                item_label = _format_99k_price_item_plain(session.get("price_item"))
                priority_embed = discord.Embed(
                    title="Priority spot available",
                    description=(
                        f"Normal: {base_price} {item_label}(s)\n"
                        f"Priority: {priority_price} {item_label}(s)"
                    ),
                    color=discord.Color.blurple(),
                )
                await interaction.followup.send(
                    embed=priority_embed,
                    view=Jump99kPriorityOfferView(
                        session_id=self.session_id,
                        signup_id=int(buyer_signup["id"]),
                        buyer_discord_id=interaction.user.id,
                    ),
                    ephemeral=True,
                )
                return

            host_discord_id = int(session.get("host_discord_id") or 0)
            host_label = await _resolve_99k_host_label(users_repo, host_discord_id)
            price_amount = int(session.get("price_amount") or 0)
            item_label = _format_99k_price_item_label(session.get("price_item"))
            reserve_embed = discord.Embed(
                title="Spot Reserved",
                description=(
                    "Spot reserved.\n"
                    f"Send **{price_amount}x {item_label}** to **{host_label}** in Torn, then press **Verify Payment**."
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(
                embed=reserve_embed,
                view=Jump99kUserControlsView(self.session_id),
                ephemeral=True,
            )
        except asyncpg.UndefinedColumnError as exc:
            log.exception(
                "99k join failed due to missing DB column session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            if "reserved_until" in str(exc):
                await interaction.followup.send(
                    "A required database migration is missing (`reserved_until`). Please ask an admin to run the latest migration SQL.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send("Sorry—could not process signup right now. Please try again.", ephemeral=True)
        except Exception:
            log.exception(
                "99k join failed session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send("Sorry—could not process signup right now. Please try again.", ephemeral=True)


class Jump99kPriorityHostToggleView(discord.ui.View):
    def __init__(self, *, session_id: int, host_discord_id: int):
        super().__init__(timeout=600)
        self.session_id = int(session_id)
        self.host_discord_id = int(host_discord_id)

    def bind_custom_ids(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                action = "yes" if child.label == "YES" else "no"
                child.custom_id = f"jump99k_priority_host:{action}:{self.session_id}"

    async def _apply(self, interaction: discord.Interaction, enabled: bool) -> None:
        if int(interaction.user.id) != self.host_discord_id:
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return

        custom_id = str((interaction.data or {}).get("custom_id") or "")
        parts = custom_id.split(":")
        if len(parts) != 3 or parts[0] != "jump99k_priority_host" or not parts[2].isdigit() or int(parts[2]) != self.session_id:
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return

        repo = JumpsRepository(get_pool())
        await repo.set_priority_enabled(session_id=self.session_id, enabled=enabled)
        for child in self.children:
            child.disabled = True

        confirmation = "Priority spot has been enabled for this jump." if enabled else "Priority spot has been disabled for this jump."
        await interaction.response.edit_message(content=confirmation, view=self)
        await _refresh_99k_panel(interaction.client, self.session_id)

    @discord.ui.button(label="YES", style=discord.ButtonStyle.success, custom_id="jump99k_priority_host:yes:0")
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._apply(interaction, True)

    @discord.ui.button(label="NO", style=discord.ButtonStyle.secondary, custom_id="jump99k_priority_host:no:0")
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._apply(interaction, False)


class Jump99kPriorityOfferView(discord.ui.View):
    def __init__(self, *, session_id: int, signup_id: int, buyer_discord_id: int):
        super().__init__(timeout=300)
        self.session_id = int(session_id)
        self.signup_id = int(signup_id)
        self.buyer_discord_id = int(buyer_discord_id)
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                action = "accept" if "Accept" in (child.label or "") else "decline"
                child.custom_id = f"jump99k_priority_offer:{action}:{self.session_id}"

    async def _continue(self, interaction: discord.Interaction, priority_reserved: bool, note: str | None = None) -> None:
        repo = JumpsRepository(get_pool())
        users_repo = UsersRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session:
            await interaction.response.send_message("Session not found.", ephemeral=True)
            return

        host_discord_id = int(session.get("host_discord_id") or 0)
        host_label = await _resolve_99k_host_label(users_repo, host_discord_id)
        price_amount = int(session.get("price_amount") or 0)
        priority_increment = int(session.get("priority_increment") or 1)
        item_label = _format_99k_price_item_plain(session.get("price_item"))
        target_amount = price_amount + priority_increment if priority_reserved else price_amount

        reserve_embed = discord.Embed(
            title="Spot Reserved",
            description=(
                (f"{note}\n" if note else "")
                + "Spot reserved.\n"
                + f"Send **{target_amount}x {item_label}** to **{host_label}** in Torn, then press **Verify Payment**."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=reserve_embed, view=Jump99kUserControlsView(self.session_id))

    @discord.ui.button(label="Accept Priority", style=discord.ButtonStyle.success, custom_id="jump99k_priority_offer_accept")
    async def accept(self, interaction: discord.Interaction, _: discord.ui.Button):
        if int(interaction.user.id) != self.buyer_discord_id:
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if custom_id != f"jump99k_priority_offer:accept:{self.session_id}":
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return

        repo = JumpsRepository(get_pool())
        reserved = await repo.reserve_priority(
            session_id=self.session_id,
            buyer_discord_id=self.buyer_discord_id,
            signup_id=self.signup_id,
            ttl_seconds=300,
        )
        note = "Priority was just taken. Continuing with normal purchase." if not reserved else None
        await self._continue(interaction, priority_reserved=reserved, note=note)

    @discord.ui.button(label="No Thanks", style=discord.ButtonStyle.secondary, custom_id="jump99k_priority_offer_decline")
    async def decline(self, interaction: discord.Interaction, _: discord.ui.Button):
        if int(interaction.user.id) != self.buyer_discord_id:
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if custom_id != f"jump99k_priority_offer:decline:{self.session_id}":
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return
        await self._continue(interaction, priority_reserved=False)


class Jump99kSessionModal(discord.ui.Modal, title="✨ 99k Happy Jump ✨"):
    payment_type = discord.ui.TextInput(
        label="Xanax 💊 | Erotic DvD 📀",
        required=True,
        max_length=20,
        placeholder="xanax",
    )
    max_slots = discord.ui.TextInput(label="Max slots", required=True, max_length=2, placeholder="5")
    spot_price = discord.ui.TextInput(
        label="Payment amount",
        required=True,
        max_length=4,
        placeholder="99",
    )
    possible_tct_start = discord.ui.TextInput(
        label="Start (optional)",
        required=False,
        max_length=48,
        placeholder="MM-DD 5:30pm, 2026-02-20 20:00 -0700, <t:1760000000:F>, in 90m",
    )
    notes = discord.ui.TextInput(label="Notes", placeholder="Add jump instructions", required=False, style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, settings: dict, session: dict | None = None):
        super().__init__()
        self.settings = settings
        self.session = session

        def _parse_slots(value, *, fallback: int | None = None) -> int | None:
            try:
                parsed = int(str(value).strip())
            except (TypeError, ValueError):
                return fallback
            if 1 <= parsed <= 7:
                return parsed
            return fallback

        default_slots = _parse_slots(settings.get("default_max_slots"), fallback=5)
        prefill_slots = default_slots
        if session:
            prefill_slots = _parse_slots(session.get("max_slots"), fallback=default_slots)

        prefill_text = str(prefill_slots)
        self.max_slots.default = prefill_text
        self.max_slots.placeholder = prefill_text

    async def on_submit(self, interaction: discord.Interaction):
        repo = JumpsRepository(get_pool())
        try:
            title = "✨ 99k Happy Jump ✨"
            try:
                slots = int(str(self.max_slots.value).strip())
            except ValueError:
                await interaction.response.send_message(embed=create_error_embed("Invalid max slots", "Max slots must be a number from 1 to 7."), ephemeral=True)
                return
            if slots < 1 or slots > 7:
                await interaction.response.send_message(embed=create_error_embed("Invalid max slots", "Max slots must be from 1 to 7."), ephemeral=True)
                return

            try:
                raw_payment_type = parse_payment_type(str(self.payment_type.value), allow_free=False)
            except ValueError as exc:
                await interaction.response.send_message(embed=create_error_embed("Invalid payment type", str(exc)), ephemeral=True)
                return

            try:
                price_amount = int(str(self.spot_price.value).strip())
            except ValueError:
                await interaction.response.send_message(embed=create_error_embed("Invalid spot price", "Spot price must be a whole number from 1 to 50."), ephemeral=True)
                return
            if price_amount < 1 or price_amount > 50:
                await interaction.response.send_message(embed=create_error_embed("Invalid spot price", "Spot price must be between 1 and 50."), ephemeral=True)
                return

            users_repo = UsersRepository(get_pool())
            host_row = await users_repo.get_user_api_key(int(interaction.user.id))
            host_timezone_name = str((host_row or {}).get("timezone_name") or "").strip() or None
            try:
                start_time, scheduled, used_utc_fallback = _parse_optional_session_start(
                    self.possible_tct_start.value,
                    self.settings,
                    host_timezone_name=host_timezone_name,
                )
            except ValueError:
                await interaction.response.send_message(
                    "Invalid start time. Use MM-DD 8:00pm, include an offset like -0700, or paste a Discord timestamp like <t:...>.",
                    ephemeral=True,
                )
                return

            notes = str(self.notes.value).strip() or None
            announce_channel_id = self.settings.get("announce_channel_id")
            if self.session:
                await repo.update_session(int(self.session["id"]), title=title, scheduled_start_text=scheduled, start_time=start_time, max_slots=slots, notes=notes, price_item=raw_payment_type, price_amount=price_amount)
                session_id = int(self.session["id"])
            else:
                session_id = await repo.create_session(
                    guild_id=interaction.guild_id,
                    host_discord_id=interaction.user.id,
                    title=title,
                    scheduled_start_text=scheduled,
                    start_time=start_time,
                    max_slots=slots,
                    notes=notes,
                    price_item=raw_payment_type,
                    price_amount=price_amount,
                    announce_channel_id=announce_channel_id,
                    announce_message_id=None,
                )

                host_snapshot = await _fetch_and_upsert_host_readiness_snapshot(
                    repo=repo,
                    users_repo=users_repo,
                    session_id=int(session_id),
                    guild_id=int(interaction.guild_id),
                    host_discord_id=int(interaction.user.id),
                )
                if host_snapshot is None:
                    await repo.upsert_readiness_snapshot(
                        session_id=int(session_id),
                        guild_id=int(interaction.guild_id),
                        discord_id=int(interaction.user.id),
                        energy=0,
                        energy_max=0,
                        drug_cooldown=0,
                        booster_cooldown=0,
                        status_text="unknown",
                    )

                if bool(self.settings.get("host_tax_enabled")):
                    host_tax_repo = HostTaxRepository(get_pool())
                    tax_type = str(self.settings.get("host_tax_type") or "").strip().lower()
                    recipient = int(self.settings.get("host_tax_recipient_torn_id") or 0)
                    item_id = int(self.settings.get("host_tax_item_id") or 0) if self.settings.get("host_tax_item_id") is not None else None
                    quantity = int(self.settings.get("host_tax_quantity") or 0) if self.settings.get("host_tax_quantity") is not None else None
                    cash_amount = int(self.settings.get("host_tax_cash_amount") or 0) if self.settings.get("host_tax_cash_amount") is not None else None
                    if recipient > 0 and tax_type in {"item", "cash"}:
                        since_dt = datetime.now(timezone.utc) - timedelta(minutes=HOST_TAX_VERIFY_WINDOW_MINUTES)
                        await host_tax_repo.attach_latest_receipt_to_session(
                            guild_id=int(interaction.guild_id),
                            discord_user_id=int(interaction.user.id),
                            session_id=int(session_id),
                            recipient_torn_id=recipient,
                            tax_type=tax_type,
                            item_id=item_id if tax_type == "item" else None,
                            quantity=quantity if tax_type == "item" else None,
                            cash_amount=cash_amount if tax_type == "cash" else None,
                            since_dt=since_dt,
                        )

                if interaction.guild and isinstance(interaction.user, discord.Member):
                    overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        interaction.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                    }
                    for role_id in GuildSettingsRepository.resolve_admin_role_ids(self.settings):
                        role = interaction.guild.get_role(int(role_id))
                        if role:
                            overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=True)

                    host_row = await users_repo.get_user_api_key(int(interaction.user.id))
                    host_torn_name = str((host_row or {}).get("torn_name") or "").strip() or "User"
                    existing_names = {c.name for c in interaction.guild.channels}
                    channel_name = _build_99k_private_channel_name(host_torn_name, int(session_id), existing_names)

                    category = interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None
                    private_channel = await interaction.guild.create_text_channel(
                        name=channel_name,
                        category=category,
                        overwrites=overwrites,
                        reason="99k jump session channel",
                    )

                    panel_embed, panel_view = await build_roster_panel(int(session_id), private_channel)
                    roster_msg = await private_channel.send(embed=panel_embed, view=panel_view)
                    await repo.set_private_channel(session_id, channel_id=private_channel.id, roster_message_id=roster_msg.id)
                    await repo.set_roster_panel_message(
                        int(session_id),
                        channel_id=int(private_channel.id),
                        message_id=int(roster_msg.id),
                    )
                    await repo.touch_roster_refreshed(int(session_id))

            target_channel_id = int(announce_channel_id) if announce_channel_id else (interaction.channel.id if interaction.channel else None)
            await upsert_99k_announcement(
                bot=interaction.client,
                repo=repo,
                guild_id=int(interaction.guild_id),
                session_id=int(session_id),
                channel_id=target_channel_id,
            )
            if not self.session:
                created_session = await repo.get_session(int(session_id))
                if created_session:
                    await post_99k_jump_created_announcement(
                        bot=interaction.client,
                        guild_id=int(interaction.guild_id),
                        session=created_session,
                        settings=self.settings,
                    )
            verb = "updated" if self.session else "created"
            await interaction.response.send_message(embed=create_success_embed("99k session saved", f"Session #{session_id} {verb}."), ephemeral=True)
            if used_utc_fallback and str(self.possible_tct_start.value or "").strip():
                await interaction.followup.send(
                    "Timezone not set; entered times are treated as UTC. Run /set_timezone to fix.",
                    ephemeral=True,
                )

            if not self.session:
                created_session = await repo.get_session(int(session_id))
                if created_session:
                    base_price = int(created_session.get("price_amount") or 0)
                    priority_increment = int(created_session.get("priority_increment") or 1)
                    priority_price = base_price + priority_increment
                    payment_item_display_name = _format_99k_price_item_plain(created_session.get("price_item"))
                    host_priority_view = Jump99kPriorityHostToggleView(
                        session_id=int(session_id),
                        host_discord_id=int(interaction.user.id),
                    )
                    host_priority_view.bind_custom_ids()
                    await interaction.followup.send(
                        content=f"Would you like to offer a priority spot on this jump for {priority_price} {payment_item_display_name}(s)?",
                        view=host_priority_view,
                        ephemeral=True,
                    )
        except Exception as e:
            log.exception("99k session modal submit failed: %s", e)
            err = create_error_embed("99k start failed", f"{type(e).__name__}: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(embed=err, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err, ephemeral=True)




jump99k_group = app_commands.Group(name="99k", description="99k happy jump commands")


@jump99k_group.command(name="start", description="Create a 99k jump session")
async def jump99k_start(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return

    if not bool(settings.get("host_tax_enabled")):
        await interaction.response.send_modal(Jump99kSessionModal(settings, session=None))
        return

    recipient = int(settings.get("host_tax_recipient_torn_id") or 0)
    requirement = _host_tax_requirement_text(settings)
    content = (
        "**Tax Required**\n"
        f"Recipient Torn ID: **{recipient or 'Not set'}**\n"
        f"Required payment: **{requirement}**\n\n"
        "Send it in Torn, then press **Verify Tax Payment**."
    )
    await interaction.response.send_message(content, view=HostTaxGateView(guild_id=interaction.guild_id, host_discord_id=interaction.user.id), ephemeral=True)


@jump99k_group.command(name="edit", description="Edit an open 99k jump session")
@app_commands.describe(jump_id="Existing jump session ID")
async def jump99k_edit(interaction: discord.Interaction, jump_id: int):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return

    if int(jump_id) <= 0:
        await interaction.response.send_message(
            embed=create_error_embed("Invalid Jump ID", "Jump ID must be a positive number."),
            ephemeral=True,
        )
        return

    repo = JumpsRepository(get_pool())
    session = await repo.get_session(int(jump_id))
    if not session or int(session.get("guild_id") or 0) != int(interaction.guild_id or 0):
        await interaction.response.send_message(
            embed=create_error_embed("Not found", "Session not found for this server."),
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(Jump99kSessionModal(settings, session=session))


@jump99k_group.command(name="list", description="List 99k sessions and readiness")
async def jump99k_list(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    await interaction.response.defer(ephemeral=True)

    repo = JumpsRepository(get_pool())
    sessions = await repo.list_open_sessions_for_guild(int(interaction.guild_id))
    if not sessions:
        await interaction.followup.send(embed=create_info_embed("99k sessions", "No open sessions."), ephemeral=True)
        return

    summary_lines = []
    for session in sessions[:10]:
        signup_count = await repo.signup_count(int(session["id"]))
        scheduled = _format_session_start_ts(session, "F")
        summary_lines.append(
            f"`#{session['id']}` **{session.get('title') or 'Untitled'}** • Start: {scheduled} • "
            f"Signups: {signup_count}/{session.get('max_slots') or 0} • Host: <@{session.get('host_discord_id')}>"
        )

    newest = sessions[0]
    readiness_rows = await repo.list_signups_with_readiness(int(newest["id"]))
    readiness_lines = []
    for row in readiness_rows[:25]:
        ready = int(row.get("energy") or 0) == 1000 and int(row.get("drug_cooldown") or 0) == 0
        marker = "🟢" if ready else "🔴"
        if row.get("overdose_flag"):
            marker = f"{marker}🟠"
        readiness_lines.append(
            f"{marker} <@{row['discord_id']}> • E {row.get('energy') or 0}/{row.get('energy_max') or 0} • "
            f"CD {row.get('drug_cooldown') or 0}s • {row.get('status_text') or 'unknown'}"
        )

    description = "\n".join(summary_lines)
    if len(sessions) > 10:
        description += f"\n…and {len(sessions) - 10} more open session(s)."

    embed = create_info_embed("99k open sessions", description)
    readiness_value = "\n".join(readiness_lines) if readiness_lines else "No signups yet."
    embed.add_field(name=f"Newest session readiness (#{newest['id']})", value=readiness_value, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


@jump99k_group.command(name="end", description="End a specific 99k session by ID")
@app_commands.describe(jump_id="99k session ID to end")
async def jump99k_end(interaction: discord.Interaction, jump_id: int):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return
    await interaction.response.defer(ephemeral=True)

    repo = JumpsRepository(get_pool())
    session = await repo.get_session(int(jump_id))
    if not session:
        await interaction.followup.send(
            embed=create_error_embed("99k end", f"Session #{int(jump_id)} not found."),
            ephemeral=True,
        )
        return
    if int(session["guild_id"]) != int(interaction.guild_id):
        await interaction.followup.send(
            embed=create_error_embed("99k end", f"Session #{int(jump_id)} is not in this server."),
            ephemeral=True,
        )
        return

    if session.get("status") == "open":
        rows = await repo.list_signups(int(session["id"]))
        completed_ids = [int(r["discord_id"]) for r in rows if r.get("status") == "signed_up"]
        ok = await repo.close_session_and_record(
            session_id=int(session["id"]),
            guild_id=int(interaction.guild_id),
            completed_discord_ids=completed_ids,
            not_completed_discord_ids=[],
        )
        if not ok:
            await interaction.followup.send(
                embed=create_error_embed("Could not close", f"Could not close session #{int(session['id'])}."),
                ephemeral=True,
            )
            return

    await _refresh_roster_if_exists(interaction.client, int(session["id"]))
    await _disable_99k_session_messages(interaction.client, session, status_text="Session closed")

    purchase_deleted = False
    channel_deleted = False
    private_channel_id = session.get("private_channel_id")

    roster_message_id = session.get("roster_message_id")
    guild = interaction.guild
    if guild is None:
        log.warning("Guild missing during 99k cleanup for session %s", session.get("id"))
    elif private_channel_id and roster_message_id:
        try:
            private_channel = guild.get_channel(int(private_channel_id))
            if private_channel is None:
                private_channel = await guild.fetch_channel(int(private_channel_id))
            message = await private_channel.fetch_message(int(roster_message_id))
            await message.delete(reason=f"99k session {session['id']} ended: remove purchase panel")
            purchase_deleted = True
        except discord.NotFound:
            purchase_deleted = True
        except discord.Forbidden:
            purchase_deleted = False
        except Exception:
            purchase_deleted = False
            log.exception("Failed to remove purchase panel for 99k session %s", session.get("id"))

    if guild and private_channel_id:
        try:
            private_channel = guild.get_channel(int(private_channel_id))
            if private_channel is None:
                private_channel = await guild.fetch_channel(int(private_channel_id))
            await private_channel.delete(reason=f"99k session {session['id']} ended")
            channel_deleted = True
        except discord.NotFound:
            channel_deleted = True
        except discord.Forbidden:
            channel_deleted = False
        except Exception:
            channel_deleted = False
            log.exception("Failed to delete private 99k channel for session %s", session.get("id"))

    await repo.clear_private_channel(int(session["id"]))

    await repo.mark_cleaned(int(session["id"]))
    await interaction.followup.send(
        embed=create_success_embed(
            "99k session ended",
            f"Ended session #{session['id']}. Private channel deleted: {'✅' if channel_deleted else '❌'}. "
            f"Purchase panel removed: {'✅' if purchase_deleted else '❌'}.",
        ),
        ephemeral=True,
    )


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
        entries = await AuditRepository(db.pool).get_audit_logs(guild_id=interaction.guild_id, limit=min(limit, 20))
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
    """Background cleanup task for 99k private channels and stale buttons."""
    try:
        repo = JumpsRepository(get_pool())
        sessions = await repo.list_non_open_sessions_for_cleanup()
        for session in sessions:
            await _disable_99k_session_messages(bot, session, status_text=f"Session {session.get('status')}")
            guild = bot.get_guild(int(session["guild_id"]))
            private_channel_id = session.get("private_channel_id")
            if private_channel_id and guild:
                try:
                    private_channel = await guild.fetch_channel(int(private_channel_id))
                    await private_channel.delete(reason="99k session finished")
                except Exception:
                    log.exception("Failed cleanup delete for 99k private channel session=%s", session.get("id"))
                await repo.clear_private_channel(int(session["id"]))
            await repo.mark_cleaned(int(session["id"]))
    except Exception:
        log.exception("cleanup_worker failed")


@cleanup_worker.before_loop
async def before_cleanup_worker():
    await bot.wait_until_ready()


@tasks.loop(seconds=15)
async def roster_panel_refresh_worker():
    """Refresh active 99k roster panels and button states."""
    try:
        repo = JumpsRepository(get_pool())
        sessions = await repo.list_active_sessions_with_roster_panel()
        for session in sessions:
            session_id = int(session["id"])
            roster_channel_id = session.get("roster_channel_id")
            roster_message_id = session.get("roster_message_id")
            if not roster_channel_id or not roster_message_id:
                continue

            try:
                channel = bot.get_channel(int(roster_channel_id))
                if channel is None:
                    channel = await bot.fetch_channel(int(roster_channel_id))

                message = await channel.fetch_message(int(roster_message_id))
                embed, view = await build_roster_panel(session_id, channel)
                await message.edit(embed=embed, view=view)
                await repo.touch_roster_refreshed(session_id)
            except (discord.NotFound, discord.Forbidden):
                await repo.clear_roster_panel_message(session_id)
            except discord.HTTPException:
                log.warning("Roster auto-refresh HTTPException session=%s", session_id)
            except Exception:
                log.exception("Roster auto-refresh failed session=%s", session_id)
            finally:
                await asyncio.sleep(0.2)
    except Exception:
        log.exception("roster_panel_refresh_worker failed")


@roster_panel_refresh_worker.before_loop
async def before_roster_panel_refresh_worker():
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
                        log.warning("Skipping readiness refresh due to missing API key discord_id=%s guild_id=%s", discord_id, guild.id)
                        continue

                    key_data = dict(key_row)
                    # Compatibility: some environments use api_key_encrypted, others encrypted_key.
                    if "encrypted_key" not in key_data and "api_key_encrypted" in key_data:
                        key_data["encrypted_key"] = key_data["api_key_encrypted"]

                    api_key = security.decrypt_api_key(key_data["encrypted_key"])
                    user_data = await torn_api.get_user_data(api_key)

                    energy = int((user_data or {}).get("bars", {}).get("energy", {}).get("current", 0) or 0)
                    energy_max = int((user_data or {}).get("bars", {}).get("energy", {}).get("maximum", 0) or 0)
                    drug_cd = int((user_data or {}).get("cooldowns", {}).get("drug", 0) or 0)
                    booster_cd = int((user_data or {}).get("cooldowns", {}).get("booster", 0) or 0)

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
                        booster_cooldown=booster_cd,
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


@tasks.loop(seconds=30)
async def auto_verify_99k_payments():
    try:
        db = get_database()
        repo = JumpsRepository(get_pool())
        users_repo = UsersRepository(db.pool)
        security = get_security_manager()
        torn_api = get_torn_api()

        await repo.cancel_expired_unpaid()
        pending = await repo.list_pending_payment_signups(limit=50)
        receipts = PaymentReceiptService(db.pool)

        for signup in pending:
            try:
                if signup.get("reserved_until") and signup["reserved_until"] <= datetime.now(timezone.utc):
                    continue
                participant_id = int(signup["participant_discord_id"])
                session_id = int(signup["session_id"])

                key_row = await users_repo.get_user_api_key(participant_id)
                encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
                if not key_row or not encrypted_key:
                    log.warning("Skipping auto_verify_99k_payments due to missing API key discord_id=%s guild_id=%s", participant_id, signup.get("guild_id"))
                    continue
                host_key = await users_repo.get_user_api_key(int(signup["host_discord_id"]))
                host_torn_id = int(host_key["torn_user_id"]) if host_key and host_key.get("torn_user_id") else 0
                if not host_torn_id:
                    continue

                api_key = security.decrypt_api_key(encrypted_key)
                since_ts = int((signup["created_at"] - timedelta(seconds=60)).timestamp())
                base_amount = int(signup.get("price_amount") or 0)
                priority_increment = int(signup.get("priority_increment") or 1)
                priority_amount = base_amount + priority_increment
                item = str(signup.get("price_item", "")).lower()
                if item == "xanax":
                    payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                    paid_amount = priority_amount if payment else base_amount
                    if not payment:
                        payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
                elif item == "erotic_dvd":
                    payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                    paid_amount = priority_amount if payment else base_amount
                    if not payment:
                        payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
                else:
                    continue

                if not payment:
                    continue

                if paid_amount == priority_amount:
                    await repo.finalize_priority(
                        session_id=session_id,
                        buyer_discord_id=participant_id,
                        signup_id=int(signup["id"]),
                    )

                await repo.mark_signup_payment_verified(session_id=session_id, discord_id=participant_id)
                payer_torn = int(key_row.get("torn_user_id") or 0) or None
                await receipts.create_and_verify(
                    featureType="jump_99k",
                    featureRefId=session_id,
                    payer_discord_id=participant_id,
                    payer_torn_id=payer_torn,
                    payee_discord_id=int(signup["host_discord_id"]) or None,
                    payee_torn_id=host_torn_id,
                    amount=paid_amount,
                    currency_type=str(signup["price_item"]),
                    metadata=payment,
                    verifier_discord_id=participant_id,
                    verifier_torn_id=payer_torn,
                )
                guild = bot.get_guild(int(signup["guild_id"]))
                if guild:
                    verified_session = await repo.get_session(session_id)
                    if verified_session:
                        await _grant_private_channel_access(guild, verified_session, participant_id)

                user = bot.get_user(participant_id)
                if user:
                    try:
                        await user.send(f"✅ Payment verified for 99k session #{session_id}")
                    except Exception:
                        pass
                await _refresh_99k_panel(bot, session_id)
                await _refresh_roster_if_exists(bot, session_id)
            except (TornAPIRateLimitError, TornAPIError):
                continue
            except Exception as entry_err:
                log.warning("Auto verify failed for signup %s/%s: %s", signup.get("session_id"), signup.get("participant_discord_id"), entry_err)
    except Exception as e:
        log.error(f"auto_verify_99k_payments error: {e}", exc_info=True)


@auto_verify_99k_payments.before_loop
async def before_auto_verify_99k_payments():
    await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def overdose_monitor():
    """Track overdose events for open 99k sessions using shared overdose tracker."""
    try:
        db = get_database()
        jumps_repo = JumpsRepository(db.pool)
        users_repo = UsersRepository(db.pool)
        tracker = OverdoseTracker(
            users_repo=users_repo,
            overdose_repo=OverdoseRepository(db.pool),
            jumps_repo=jumps_repo,
        )

        sessions = await jumps_repo.list_open_sessions()
        now = datetime.now(timezone.utc)
        for session in sessions:
            session_id = int(session["id"])
            guild_id = int(session["guild_id"])
            guild = bot.get_guild(guild_id)
            signups = await jumps_repo.list_signups(session_id)
            for signup in signups:
                if not bool(signup.get("payment_verified")):
                    continue
                if signup.get("status") not in {"signed_up", "completed", "not_completed"}:
                    continue
                verified_at = signup.get("payment_verified_at")
                if not verified_at:
                    continue

                discord_id = int(signup["discord_id"])
                key = (session_id, guild_id, discord_id)
                last_checked = _od_last_checked.get(key)
                if last_checked and (now - last_checked).total_seconds() < 60:
                    continue
                _od_last_checked[key] = now

                try:
                    event = await tracker.check_user_since(
                        guild_id=guild_id,
                        discord_id=discord_id,
                        since_ts=int(verified_at.timestamp()),
                        session_id=session_id,
                    )
                    if not event or not event.get("session_marked"):
                        continue

                    notice = (
                        f"⚠️ 99k OD detected in session #{session_id}. User ID: {discord_id}. "
                        f"Type: {event.get('event_type')}. Log: {event.get('torn_log_id')}"
                    )

                    sent = False
                    if guild is not None and session.get("private_channel_id"):
                        try:
                            ch = guild.get_channel(int(session["private_channel_id"])) or await guild.fetch_channel(int(session["private_channel_id"]))
                            await ch.send(notice)
                            sent = True
                        except Exception:
                            sent = False
                    if not sent:
                        try:
                            host_user = bot.get_user(int(session["host_discord_id"])) or await bot.fetch_user(int(session["host_discord_id"]))
                            await host_user.send(notice)
                        except Exception:
                            pass

                    insurer_id = await jumps_repo.get_selected_insurer_for_signup(session_id=session_id, discord_id=discord_id)
                    if insurer_id:
                        try:
                            insurer = bot.get_user(int(insurer_id)) or await bot.fetch_user(int(insurer_id))
                            await insurer.send(notice)
                        except Exception:
                            pass
                except OverdoseTrackerError as exc:
                    log.warning("OD tracker Torn/API failure session=%s user=%s: %s", session_id, discord_id, exc)
                except Exception:
                    log.exception("OD tracker failed session=%s user=%s", session_id, discord_id)

                await asyncio.sleep(0.2)
    except Exception as e:
        log.error(f"Overdose monitor error: {e}", exc_info=True)


@overdose_monitor.before_loop
async def before_overdose_monitor():
    await bot.wait_until_ready()


@tasks.loop(seconds=config.INSURANCE_CHECK_INTERVAL)
async def insurance_monitor():
    """Legacy monitor disabled: OD notifications are handled by overdose_monitor."""
    return


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
        
        settings = await GuildSettingsRepository(db).get_or_create(raffle['guild_id'])
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
            await GuildSettingsRepository(db).upsert_settings(raffle['guild_id'], raffle_channel_id=None)
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
