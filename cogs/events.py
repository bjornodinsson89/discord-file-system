"""
Happy Jump Discord Bot - Discord-only service
Discord bot process entrypoint (no embedded web server).
"""

import discord
import asyncpg
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
import logging
import ssl
import asyncio
import json
import math
import re
import uuid
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Optional

import config
from constants.jump99k import SIGNUP_ACTIVE_STATUSES
from utils import init_database, get_database, init_torn_api, get_torn_api, init_security, get_security_manager, GuildSettingsRepository, require_api_key
from utils.database import (
    DatabaseAcquireTimeoutError,
    get_pool,
    is_initialized as db_is_initialized,
    wait_until_initialized,
)
from utils.migrations import run_migrations
from utils.db_acquire import acquire_conn
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
from utils.discord_safe_send import safe_send_channel
from utils.command_checks import CommandAccessError, has_role_hierarchy_access, require_command_access
from utils.advisory_lock import run_with_advisory_lock
from utils.worker_throttle import db_heavy_worker_slot, sleep_startup_jitter
from utils.panel_edit_safety import PANEL_EDIT_SAFETY
from utils.redaction import redact_text
from setup_panel import (
    DEFAULT_WELCOME_TEMPLATE,
    detect_rules_channel,
    has_setup_permission,
    render_welcome_template,
    send_setup_panel,
    InsurerProfileModal,
)
from cogs import EXTENSIONS
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
from repositories.api_audit_repository import ApiAuditRepository
from repositories.user_torn_identity_cache import UserTornIdentityCacheRepository
from repositories.applications import ApplicationsRepository
from services.payment_receipts import PaymentReceiptService
from services.permissions import validate_99k_permissions
from services.discord_cleanup import delete_message_safe, delete_channel_safe
from services.logging_utils import log_event
from services.torn_identity import parse_member_torn_identity_from_nickname
from services.member_cleanup import MemberCleanupService
from repositories.jumps import SignupStatusSchemaMismatchError

log = logging.getLogger("happy_jumper")


HOST_TAX_VERIFY_WINDOW_MINUTES = 30
TORN_NAME_CACHE_TTL_MINUTES = 10
_TORN_NAME_CACHE: dict[int, tuple[str, datetime]] = {}
_TORN_NAME_FAIL_LOG_CACHE: dict[int, datetime] = {}
_READINESS_MISSING_KEY_LOG_CACHE: dict[tuple[int, int], datetime] = {}
_READINESS_PERMISSION_LOG_CACHE: dict[tuple[int, int], datetime] = {}
_WORKER_DB_WAIT_LOGGED: set[str] = set()
_READINESS_SESSION_NEXT_DUE: dict[int, datetime] = {}
_JUMP_AUTOMATION_STATE: dict[int, dict[str, object]] = {}
_WHO_CAN_JUMP_REFRESH_LOCKS: dict[int, asyncio.Lock] = {}
_WHO_CAN_JUMP_LAST_RENDER: dict[int, dict[str, object]] = {}
_WHO_CAN_JUMP_LAST_MANUAL_REFRESH: dict[int, datetime] = {}
_WHO_CAN_JUMP_MANUAL_REFRESH_COOLDOWN_SECONDS = 60
_READINESS_FETCH_CACHE_TTL_SECONDS = 5
_READINESS_FETCH_CACHE: dict[tuple[int, int], tuple[datetime, dict]] = {}
_WHO_CAN_JUMP_READINESS_CACHE_TTL_SECONDS = 5
_WHO_CAN_JUMP_READINESS_CACHE: dict[tuple[int, int], tuple[datetime, dict]] = {}


def _automation_state(session_id: int) -> dict[str, object]:
    return _JUMP_AUTOMATION_STATE.setdefault(
        int(session_id),
        {
            "running": False,
            "paused": False,
            "active_discord_id": None,
            "active_position": None,
            "saw_nonzero_energy": False,
            "consecutive_low_energy_polls": 0,
            "last_transition_at": None,
        },
    )



async def _resolve_bot_member(guild: discord.Guild) -> discord.Member:
    if bot.user is None:
        raise RuntimeError("Bot user unavailable while resolving guild member")
    bot_member = guild.get_member(bot.user.id)
    if bot_member is not None:
        return bot_member
    try:
        fetched = await guild.fetch_member(bot.user.id)
    except Exception as exc:
        log.exception("Failed to fetch bot member for guild %s", guild.id)
        raise RuntimeError(f"Unable to resolve bot member in guild {guild.id}") from exc
    if fetched is None:
        raise RuntimeError(f"Unable to resolve bot member in guild {guild.id}")
    return fetched


async def _worker_db_ready(worker_name: str) -> bool:
    if db_is_initialized():
        _WORKER_DB_WAIT_LOGGED.discard(worker_name)
        return True
    if worker_name not in _WORKER_DB_WAIT_LOGGED:
        log.debug("%s waiting for database pool initialization", worker_name)
        _WORKER_DB_WAIT_LOGGED.add(worker_name)
    return False


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
    async with acquire_conn(pool, config.DB_ACQUIRE_TIMEOUT) as conn:
        return bool(await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key))


async def release_advisory_lock(pool, lock_key: int) -> bool:
    async with acquire_conn(pool, config.DB_ACQUIRE_TIMEOUT) as conn:
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
    return await _fetch_and_upsert_user_readiness_snapshot(
        repo=repo,
        users_repo=users_repo,
        session_id=session_id,
        guild_id=guild_id,
        discord_id=host_discord_id,
    )


async def _fetch_and_upsert_user_readiness_snapshot(
    *,
    repo: JumpsRepository,
    users_repo: UsersRepository,
    session_id: int,
    guild_id: int,
    discord_id: int,
) -> dict | None:
    """Fetch readiness from Torn and upsert snapshot if an API key is available."""
    key_row = await users_repo.get_user_api_key(discord_id)
    encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
    if not encrypted_key:
        return None

    cache_key = (int(session_id), int(discord_id))
    now = datetime.now(timezone.utc)
    cached = _READINESS_FETCH_CACHE.get(cache_key)
    if cached and (now - cached[0]).total_seconds() <= _READINESS_FETCH_CACHE_TTL_SECONDS:
        cached_payload = dict(cached[1])
        try:
            await repo.upsert_readiness_snapshot(
                session_id=session_id,
                guild_id=guild_id,
                discord_id=discord_id,
                energy=int(cached_payload.get("energy") or 0),
                energy_max=int(cached_payload.get("energy_max") or 0),
                drug_cooldown=int(cached_payload.get("drug_cooldown") or 0),
                booster_cooldown=int(cached_payload.get("booster_cooldown") or 0),
                status_text=str(cached_payload.get("status_text") or "not ready"),
            )
        except Exception:
            log.exception(
                "Readiness snapshot upsert failure from cache guild_id=%s session_id=%s discord_id=%s",
                guild_id,
                session_id,
                discord_id,
            )
            return None
        return cached_payload

    try:
        api_key = get_security_manager().decrypt_api_key(encrypted_key)
        user_data = await get_torn_api().get_user_data(
            api_key,
            audit_discord_id=int(discord_id),
            audit_torn_id=int((key_row or {}).get("torn_user_id") or 0) or None,
            audit_context="jump_readiness",
            audit_query_meta={},
        )
    except TornAPIPermissionError as exc:
        permission_status = "API key missing Bars/Cooldowns permissions"
        await repo.upsert_readiness_snapshot(
            session_id=session_id,
            guild_id=guild_id,
            discord_id=discord_id,
            energy=0,
            energy_max=0,
            drug_cooldown=0,
            booster_cooldown=0,
            status_text=permission_status,
        )
        log.info(
            "Readiness snapshot stored with permission error guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s",
            guild_id,
            session_id,
            discord_id,
            type(exc).__name__,
            exc,
        )
        return {
            "session_id": session_id,
            "guild_id": guild_id,
            "discord_id": discord_id,
            "energy": 0,
            "energy_max": 0,
            "drug_cooldown": 0,
            "booster_cooldown": 0,
            "status_text": permission_status,
        }
    except TornAPIRateLimitError as exc:
        log.debug(
            "Readiness snapshot skipped due to rate limit guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s",
            guild_id,
            session_id,
            discord_id,
            type(exc).__name__,
            exc,
        )
        return None
    except TornAPIError as exc:
        throttle_key = (session_id, discord_id)
        now = datetime.now(timezone.utc)
        expiry = _READINESS_PERMISSION_LOG_CACHE.get(throttle_key)
        if not expiry or expiry <= now:
            log.info(
                "Readiness snapshot fetch failed guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s",
                guild_id,
                session_id,
                discord_id,
                type(exc).__name__,
                exc,
            )
            _READINESS_PERMISSION_LOG_CACHE[throttle_key] = now + timedelta(hours=1)
        return None
    except Exception:
        log.exception(
            "Unexpected readiness snapshot failure guild_id=%s session_id=%s discord_id=%s",
            guild_id,
            session_id,
            discord_id,
        )
        return None

    try:
        energy_current = int((user_data or {}).get("bars", {}).get("energy", {}).get("current", 0) or 0)
        energy_max = int((user_data or {}).get("bars", {}).get("energy", {}).get("maximum", 0) or 0)
        drug_cd = int((user_data or {}).get("cooldowns", {}).get("drug", 0) or 0)
        booster_cd = int((user_data or {}).get("cooldowns", {}).get("booster", 0) or 0)
    except Exception:
        log.exception(
            "Readiness parse failure guild_id=%s session_id=%s discord_id=%s",
            guild_id,
            session_id,
            discord_id,
        )
        return None

    status_text = "ready" if energy_current >= 1000 and drug_cd == 0 else "not ready"

    try:
        await repo.upsert_readiness_snapshot(
            session_id=session_id,
            guild_id=guild_id,
            discord_id=discord_id,
            energy=energy_current,
            energy_max=energy_max,
            drug_cooldown=drug_cd,
            booster_cooldown=booster_cd,
            status_text=status_text,
        )
    except Exception:
        log.exception(
            "Readiness snapshot upsert failure guild_id=%s session_id=%s discord_id=%s",
            guild_id,
            session_id,
            discord_id,
        )
        return None
    payload = {
        "session_id": session_id,
        "guild_id": guild_id,
        "discord_id": discord_id,
        "energy": energy_current,
        "energy_max": energy_max,
        "drug_cooldown": drug_cd,
        "booster_cooldown": booster_cd,
        "status_text": status_text,
    }
    _READINESS_FETCH_CACHE[cache_key] = (datetime.now(timezone.utc), payload)
    return payload



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


async def can_manage_99k_session(interaction: discord.Interaction, session: dict | None) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    member = interaction.user
    if member.guild_permissions.administrator:
        return True

    if session and int(session.get("host_discord_id") or 0) == int(member.id):
        return True

    settings = await GuildSettingsRepository(get_database()).get_guild_settings(int(interaction.guild_id))
    host_role_id = int(settings.get("host99k_role_id") or settings.get("host_role_id") or 0)
    return host_role_id > 0 and any(int(role.id) == host_role_id for role in member.roles)


async def _can_use_manual_add_controls(interaction: discord.Interaction, session: dict | None) -> bool:
    return await can_manage_99k_session(interaction, session)


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

        target_role = interaction.guild.get_role(role_id) if interaction.guild else None
        if not target_role:
            await interaction.response.send_message(embed=create_error_embed("Invalid input", "Host role not found in this server."), ephemeral=True)
            return
        if not has_role_hierarchy_access(guild=interaction.guild, actor=interaction.user, target_role=target_role):
            await interaction.response.send_message(
                embed=create_error_embed("Role hierarchy blocked", "You cannot configure a role that is equal to or above your highest role."),
                ephemeral=True,
            )
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
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return host == "torn.com" or host.endswith(".torn.com")


def _excerpt(text: str, limit: int = 180) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _is_db_unavailable_error(exc: BaseException) -> bool:
    return isinstance(exc, (DatabaseAcquireTimeoutError, asyncpg.PostgresError, OSError, ssl.SSLError))


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


def _next_error_id() -> str:
    return uuid.uuid4().hex[:8]


async def _send_unexpected_error(interaction: discord.Interaction, *, error_id: str):
    await _send_interaction_error(
        interaction,
        f"An unexpected error occurred. Please try again. Error ID: `{error_id}`",
    )


async def _global_view_error(self, interaction: discord.Interaction, error: Exception, item):
    error_id = _next_error_id()
    log_event(
        log,
        logging.ERROR,
        "ui_interaction_unhandled_error",
        error_id=error_id,
        item_custom_id=getattr(item, "custom_id", None),
        error_type=type(error).__name__,
        error_message=redact_text(str(error)),
        exc_info=error,
    )
    await _send_unexpected_error(interaction, error_id=error_id)


async def _global_modal_error(self, interaction: discord.Interaction, error: Exception):
    error_id = _next_error_id()
    log_event(
        log,
        logging.ERROR,
        "modal_interaction_unhandled_error",
        error_id=error_id,
        error_type=type(error).__name__,
        error_message=redact_text(str(error)),
        exc_info=error,
    )
    if isinstance(error, discord.HTTPException) and getattr(error, "code", None) == 50035 and "Invalid Form Body" in str(error):
        await _send_interaction_error(
            interaction,
            "This interaction is outdated. Please re-run: /99k edit jump_id:<id> (example: /99k edit jump_id:22).",
        )
        return
    await _send_unexpected_error(interaction, error_id=error_id)


discord.ui.View.on_error = _global_view_error
discord.ui.Modal.on_error = _global_modal_error


async def setup_hook():
    """Initialize process-scoped dependencies once per bot lifecycle."""
    config.validate_config()
    await init_database()
    await run_migrations(get_pool())
    init_torn_api()
    await init_security()

    loaded_extensions: list[str] = []
    for ext in EXTENSIONS:
        await bot.load_extension(ext)
        loaded_extensions.append(ext)
        log.info("Loaded extension: %s", ext)

    admin_handlers.set_bot_instance(bot)
    log.info("Process dependencies initialized (extensions=%s)", loaded_extensions)


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
        session_id = int(session["id"])
        bot.add_view(Jump99kRosterPanelView(session_id, roster_size=Jump99kRosterPanelView.MAX_POSITIONS))
        if session.get("private_channel_id"):
            bot.add_view(Jump99kHostControlsView(session_id))


async def register_persistent_signup_views() -> None:
    """Register persistent signup views for all open sessions."""
    db = get_database()
    sessions = await JumpsRepository(db.pool).list_open_sessions()
    for session in sessions:
        session_id = int(session["id"])
        max_slots = int(session.get("max_slots") or 0)
        is_full = False
        if max_slots > 0:
            signups = await JumpsRepository(db.pool).list_signups(session_id)
            signed_up = sum(1 for row in signups if row.get("status") in SIGNUP_ACTIVE_STATUSES)
            is_full = signed_up >= max_slots
        bot.add_view(Jump99kSignupView(session_id=session_id, is_full=is_full, is_closed=False, is_locked=bool(session.get("signups_locked"))))



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
    
    async def _add_timezone_prompt_view() -> None:
        bot.add_view(TimezonePromptView())

    ready_steps = [
        ("sync_application_commands", sync_application_commands),
        ("register_persistent_application_review_views", register_persistent_application_review_views),
        ("register_persistent_roster_views", register_persistent_roster_views),
        ("register_persistent_signup_views", register_persistent_signup_views),
        ("register_persistent_pool_views", lambda: register_persistent_pool_views(bot)),
        ("register_persistent_who_can_jump_views", register_persistent_who_can_jump_views),
        ("add_timezone_prompt_view", _add_timezone_prompt_view),
    ]
    for step_name, step in ready_steps:
        try:
            await step()
        except Exception:
            log.exception("on_ready step failed: %s", step_name)

    if not db_is_initialized():
        initialized = await wait_until_initialized(timeout=30.0)
        if not initialized:
            log.error("Database pool not initialized by on_ready; skipping worker startup")
            return

    worker_steps = [
        ("start_cleanup_worker", cleanup_worker),
        ("start_readiness_worker", readiness_worker),
        ("start_jump_automation_worker", jump_automation_worker),
        ("start_overdose_monitor", overdose_monitor),
        ("start_raffle_completion_worker", raffle_completion_worker),
        ("start_auto_verify_99k_payments", auto_verify_99k_payments),
        ("start_roster_panel_refresh_worker", roster_panel_refresh_worker),
        ("start_who_can_jump_panel_worker", who_can_jump_panel_worker),
        ("start_cleanup_retry_worker", cleanup_retry_worker),
        ("start_departed_member_reconciliation_worker", departed_member_reconciliation_worker),
    ]
    started_workers = []
    for step_name, worker in worker_steps:
        try:
            if not worker.is_running():
                worker.start()
                started_workers.append(step_name)
        except Exception:
            log.exception("on_ready step failed: %s", step_name)

    log_event(log, logging.INFO, "workers_started", action="startup", result="ok", workers=started_workers)
    if "start_readiness_worker" in started_workers or readiness_worker.is_running():
        log.info("99k readiness worker running")
    log.info("✓ Bot is ready!")


async def _cache_member_nickname_identity(member: discord.Member) -> None:
    torn_id, torn_name = parse_member_torn_identity_from_nickname(member)
    if not torn_id:
        return
    try:
        repo = UserTornIdentityCacheRepository(get_pool())
        await repo.upsert_identity(
            guild_id=int(member.guild.id),
            discord_id=int(member.id),
            torn_user_id=int(torn_id),
            torn_name=torn_name,
            source="nickname",
            is_official_discord_verified=False,
            last_verified_at=None,
        )
    except Exception:
        log.debug("Failed caching nickname torn identity guild_id=%s user_id=%s", member.guild.id, member.id, exc_info=True)


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

    try:
        me = await _resolve_bot_member(guild)
    except RuntimeError:
        log.exception("Unable to resolve bot member for guild %s during guild join", guild.id)
        return

    for channel in guild.text_channels:
        perms = channel.permissions_for(me)
        if perms.send_messages and perms.embed_links:
            await repo.set_announce_channel(guild.id, channel.id)
            log.info("Auto-selected announce channel %s for guild %s", channel.id, guild.id)
            break


@bot.event
async def on_member_join(member: discord.Member):
    """Send configured welcome message when enabled for the guild."""
    if member.bot:
        return

    await _cache_member_nickname_identity(member)

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




@bot.event
async def on_member_remove(member: discord.Member):
    if member.bot:
        return
    try:
        cleanup = MemberCleanupService(get_pool())
        summary = await cleanup.cleanup_departed_member(int(member.guild.id), int(member.id))
        total = sum(int(v or 0) for v in summary.values())
        log.info("Departed-member cleanup complete guild_id=%s user_id=%s removed=%s", member.guild.id, member.id, total)
    except Exception:
        log.exception("Departed-member cleanup failed guild_id=%s user_id=%s", member.guild.id, member.id)

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if after.bot:
        return
    before_nick = str(before.nick or before.display_name or "").strip()
    after_nick = str(after.nick or after.display_name or "").strip()
    if before_nick == after_nick:
        return
    await _cache_member_nickname_identity(after)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global slash command error handler."""
    root_error = getattr(error, "original", error)
    if isinstance(root_error, CommandAccessError):
        await _send_interaction_error(interaction, root_error.user_message)
        return
    if isinstance(root_error, app_commands.MissingPermissions):
        missing = ", ".join(root_error.missing_permissions) if root_error.missing_permissions else "required permissions"
        await _send_interaction_error(interaction, f"You are missing permissions to use this command: {missing}.")
        return
    if isinstance(root_error, app_commands.BotMissingPermissions):
        missing = ", ".join(root_error.missing_permissions) if root_error.missing_permissions else "required permissions"
        await _send_interaction_error(interaction, f"I am missing permissions to complete this command: {missing}.")
        return
    if isinstance(root_error, DatabaseAcquireTimeoutError):
        log_event(
            log,
            logging.WARNING,
            "slash_command_db_acquire_timeout",
            timeout_error_type=type(root_error).__name__,
            command_name=getattr(getattr(interaction, "command", None), "name", None),
        )
        await _send_interaction_error(interaction, "Database is busy right now. Please try again in a moment.")
        return
    error_id = _next_error_id()
    log_event(
        log,
        logging.ERROR,
        "slash_command_unhandled_error",
        error_id=error_id,
        command_name=getattr(getattr(interaction, "command", None), "name", None),
        guild_id=interaction.guild_id,
        user_id=getattr(getattr(interaction, "user", None), "id", None),
        error_type=type(root_error).__name__,
        error_message=redact_text(str(root_error)),
        exc_info=root_error,
    )
    await _send_unexpected_error(interaction, error_id=error_id)


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




@bot.tree.command(name="api_audit", description="View your recent Torn API activity")
@app_commands.describe(limit="How many recent events to show (1-100)")
async def api_audit(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 100] = 25):
    await interaction.response.defer(ephemeral=True)

    rows = await ApiAuditRepository(get_pool()).list_recent(int(interaction.user.id), int(limit))
    if not rows:
        await interaction.followup.send("No API activity recorded yet.", ephemeral=True)
        return

    label_map = {
        "payment_verify_logs": "Payment verification (logs)",
        "jump_readiness": "Jump readiness (bars/cooldowns)",
        "api_key_check": "API key check",
    }

    embed = discord.Embed(title="Recent Torn API activity", color=discord.Color.blurple())
    for row in rows:
        created_unix = int(row.created_at.timestamp())
        context_label = label_map.get(row.context, row.context)
        endpoint = row.endpoint
        if row.selections:
            endpoint = f"{endpoint} (selections={row.selections})"
        http_text = str(row.http_status) if row.http_status is not None else "n/a"
        duration_text = f"{row.duration_ms}ms" if row.duration_ms is not None else "n/a"
        status_text = f"{row.status.upper()} · HTTP {http_text} · {duration_text}"

        value = (
            f"**{context_label}**\n"
            f"`{endpoint}`\n"
            f"{status_text}\n"
            f"<t:{created_unix}:R> · <t:{created_unix}:F>"
        )
        embed.add_field(name=f"Event #{row.id}", value=value[:1024], inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

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
    
    repo = JumpsRepository(db.pool)
    sessions = await repo.list_open_sessions_with_user_signup(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
    )

    user_signups = []
    user_waitlist = []
    hosted_sessions = []

    for session in sessions:
        if session['host_discord_id'] == interaction.user.id:
            hosted_sessions.append(session)

        signup_status = session.get('user_signup_status')
        if signup_status:
            user_signups.append({'session': session, 'signup': {'status': signup_status}})

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
@require_command_access(
    include_configured_admin_roles=True,
    allow_manage_guild=True,
    failure_message="You must be the guild owner, Administrator, Manage Guild, or have a configured setup admin role.",
)
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

    profile = await ApplicationsRepository(db.pool).get_insurer_profile(guild_id=interaction.guild_id, user_id=interaction.user.id)
    await interaction.response.send_modal(InsurerProfileModal(db=db, existing_profile=profile))



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
            data = await torn.get_user_data(
                api_key,
                audit_discord_id=int(discord_id),
                audit_torn_id=int(row.get("torn_user_id") or 0) or None,
                audit_context="api_key_check",
                audit_query_meta={},
            )
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

    rows: list[tuple[int, str, str, str, str | None]] = []
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
        description = item.get("description")
        if isinstance(description, str):
            description = description.strip() or None
        else:
            description = None

        rows.append((item_id, name, normalized, image_url, description))
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
    settings = await GuildSettingsRepository(get_database()).get_or_create(int(session["guild_id"]))
    await upsert_99k_announcement(
        bot=bot_client,
        repo=repo,
        guild_id=int(session["guild_id"]),
        session_id=int(session["id"]),
        channel_id=int(session["announce_channel_id"]) if session.get("announce_channel_id") else None,
        settings=settings,
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
            except discord.Forbidden:
                log.debug("No permission to edit roster message session_id=%s", session_id)
            except discord.HTTPException:
                log.warning("HTTP error editing roster message session_id=%s", session_id)
            except Exception:
                log.exception("Unexpected error editing roster message session_id=%s", session_id)


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
    signups_locked = bool(session.get("signups_locked"))
    is_full = not is_closed and max_slots > 0 and signed_up >= max_slots
    status_text = "Closed" if is_closed else ("Locked" if signups_locked else ("Full" if is_full else "Open"))
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
        f"{'🔒 Signups locked (jump started)' if signups_locked else '✅ Signups open'}\n"
        "_Click **Join** to reserve your spot._"
    )


def _resolve_99k_signup_channel_id(settings: dict | None, fallback_channel_id: int | None = None) -> int | None:
    settings = settings or {}
    for key in ("jump_99k_channel_id", "announce_channel_id"):
        raw = settings.get(key)
        try:
            channel_id = int(raw or 0)
        except (TypeError, ValueError):
            channel_id = 0
        if channel_id > 0:
            return channel_id
    try:
        fallback = int(fallback_channel_id or 0)
    except (TypeError, ValueError):
        fallback = 0
    return fallback if fallback > 0 else None


def get_announce_ids(session: dict) -> tuple[int | None, int | None]:
    channel_id = session.get("announce_channel_id")
    message_id = session.get("announce_message_id")
    return (int(channel_id) if channel_id else None, int(message_id) if message_id else None)


def build_99k_jump_created_announcement_content(session: dict, settings: dict | None = None) -> str:
    start_text = _format_session_start_display(session)
    max_slots = int(session.get("max_slots") or 0)
    price_amount = int(session.get("price_amount") or 0)
    price_item_label = _format_99k_price_item_label(session.get("price_item"))
    priority_text = _priority_status_text(session)
    jump_channel_id = _resolve_99k_signup_channel_id(settings)
    if jump_channel_id:
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




def get_99k_publication_plan(settings: dict | None) -> dict[str, bool]:
    disabled = bool((settings or {}).get("disable_99k_announcements", False))
    return {
        "upsert_signup_panel": True,
        "post_announcement": not disabled,
    }

async def post_99k_jump_created_announcement(
    bot: commands.Bot,
    guild_id: int,
    session: dict,
    settings: dict,
) -> None:
    plan = get_99k_publication_plan(settings)
    if not plan["post_announcement"]:
        return

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

    role_ids = GuildSettingsRepository._normalize_role_id_list(settings.get("jump_ping_role_ids"))
    role_mentions = " ".join(f"<@&{rid}>" for rid in role_ids)
    content = build_99k_jump_created_announcement_content(session, settings)
    prefix = f"{role_mentions}\n" if role_mentions else ""
    sent = await safe_send_channel(channel.guild if hasattr(channel, "guild") and channel.guild else bot, int(channel_id), content=f"{prefix}{content}")
    if not sent:
        log.warning(
            "99k jump announcement post skipped guild_id=%s channel_id=%s session_id=%s",
            guild_id,
            channel_id,
            session.get("id"),
        )


async def upsert_99k_announcement(
    bot: commands.Bot,
    repo: JumpsRepository,
    guild_id: int,
    session_id: int,
    channel_id: int | None,
    settings: dict | None = None,
) -> None:
    session = await repo.get_session(session_id)
    if not session or int(session.get("guild_id", 0)) != int(guild_id):
        return
    users_repo = UsersRepository(get_pool())

    signups = await repo.list_signups(session_id)
    signed_up = sum(1 for row in signups if row.get("status") in SIGNUP_ACTIVE_STATUSES)
    paid = sum(1 for row in signups if row.get("payment_verified"))
    max_slots = int(session.get("max_slots") or 0)
    is_closed = _is_99k_closed(session.get("status"))
    is_locked = bool(session.get("signups_locked"))
    is_full = not is_closed and max_slots > 0 and signed_up >= max_slots

    guild_settings = settings
    if guild_settings is None:
        guild_settings = await GuildSettingsRepository(get_database()).get_or_create(int(guild_id))

    target_channel_id = _resolve_99k_signup_channel_id(
        guild_settings,
        channel_id or session.get("announce_channel_id"),
    ) or 0
    if target_channel_id <= 0:
        return

    guild = bot.get_guild(int(guild_id))
    channel = guild.get_channel(target_channel_id) if guild else bot.get_channel(target_channel_id)
    if channel is None:
        try:
            channel = await (guild.fetch_channel(target_channel_id) if guild else bot.fetch_channel(target_channel_id))
        except Exception:
            log.exception("Failed to resolve 99k signup panel channel guild_id=%s channel_id=%s session_id=%s", guild_id, target_channel_id, session_id)
            return

    host_discord_id = int(session.get("host_discord_id") or 0)
    host_label = await _resolve_99k_host_label(users_repo, host_discord_id)
    content = build_99k_announcement_content(session, signed_up, paid, host_label)
    view = Jump99kSignupView(session_id=session_id, is_full=is_full, is_closed=is_closed, is_locked=is_locked)

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
            log.exception("Failed to edit existing 99k signup panel session_id=%s channel_id=%s message_id=%s", session_id, getattr(channel, "id", None), announce_message_id)
            return

    try:
        msg = await channel.send(content, view=view)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.warning("Failed to send/update 99k announcement session_id=%s channel_id=%s", session_id, target_channel_id, exc_info=True)
        return
    await repo.set_announcement_message(session_id, channel_id=int(channel.id), message_id=int(msg.id))

async def _refresh_99k_panel(bot_client: commands.Bot, session_id: int) -> None:
    db = get_database()
    repo = JumpsRepository(db.pool)
    try:
        session = await repo.get_session(session_id)
        if not session:
            return
        settings = await GuildSettingsRepository(get_database()).get_or_create(int(session["guild_id"]))
        await upsert_99k_announcement(
            bot=bot_client,
            repo=repo,
            guild_id=int(session["guild_id"]),
            session_id=int(session_id),
            channel_id=int(session["announce_channel_id"]) if session.get("announce_channel_id") else None,
            settings=settings,
        )
    except Exception:
        log.exception("Failed to refresh 99k signup panel session_id=%s", session_id)


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
        data = await get_torn_api().get_user_data(
            api_key,
            audit_discord_id=int(discord_id),
            audit_torn_id=int((user_row or {}).get("torn_user_id") or 0) or None,
            audit_context="api_key_check",
            audit_query_meta={},
        )
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
        return _visible_jump_state(state)

    host_name = await _display_name_for(host_id)
    host_energy = (host_readiness or {}).get("energy")
    host_energy_max = (host_readiness or {}).get("energy_max")
    host_drug_cd = (host_readiness or {}).get("drug_cooldown") if host_readiness else None
    host_booster_cd = (host_readiness or {}).get("booster_cooldown") if host_readiness else None
    host_status_text = str((host_readiness or {}).get("status_text") or "").strip()
    host_has_readiness = bool(host_readiness and host_readiness.get("checked_at"))
    host_ready = host_energy is not None and host_energy_max is not None and int(host_energy) >= 1000 and int(host_drug_cd or 0) == 0
    host_missing_permissions = host_has_readiness and host_status_text.lower().startswith("api key missing")
    host_emoji = "🟥" if host_missing_permissions else ("🟩" if host_ready else "🟥")
    if host_missing_permissions:
        host_readiness_text = host_status_text
    elif host_energy is not None and host_energy_max is not None:
        host_readiness_text = f"E-lvl {_format_energy_pair(host_energy, host_energy_max)} Dcd |{_format_cd_hhmm(host_drug_cd)}| Bcd |{_format_cd_hhmm(host_booster_cd)}|"
    else:
        host_readiness_text = "API key required"

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
        f"1) Name:{host_name} {host_readiness_text} {host_emoji} • {_state_label(host_state)}"
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
        status_text = str(row.get("status_text") or "").strip()
        missing_permissions = has_readiness and status_text.lower().startswith("api key missing")

        if bool(row.get("overdose_flag")):
            emoji = "🟧"
        elif missing_permissions:
            emoji = "🟥"
        elif energy is not None and energy >= 1000 and int(drug_cd or 0) == 0:
            emoji = "🟩"
        else:
            emoji = "🟥"

        state = participant_states[idx - 2] if idx - 2 < len(participant_states) else "waiting"
        if missing_permissions:
            readiness_text = status_text
        elif has_readiness:
            readiness_text = f"E-lvl {_format_energy_pair(energy, energy_max)} Dcd |{_format_cd_hhmm(drug_cd)}| Bcd |{_format_cd_hhmm(booster_cd)}|"
        else:
            readiness_text = "API key required"
        lines.append(
            f"{idx}) Name:{name}{priority_label} {readiness_text} {emoji} • {_state_label(state)}"
        )

    if in_progress_index is not None and in_progress_index <= len(roster_names):
        lines[1] = f"Now jumping: {roster_names[in_progress_index - 1]}"

    max_slots = int(session.get("max_slots") or 0)
    total_positions = 1 + max(0, max_slots)
    total_positions = max(1, min(total_positions, Jump99kRosterPanelView.MAX_POSITIONS))

    embed = _build_roster_embed(lines)
    view = Jump99kRosterPanelView(session_id, roster_size=total_positions)
    return embed, view


def _build_position_owner_ids(*, session: dict, signups: list[dict], total_positions: int) -> dict[int, int]:
    host_id = int(session.get("host_discord_id") or 0)
    owner_ids: dict[int, int] = {}
    if host_id > 0 and total_positions >= 1:
        owner_ids[1] = host_id

    participants = [row for row in signups if int(row.get("discord_id") or 0) != host_id]
    for position, row in enumerate(participants, start=2):
        if position > total_positions:
            break
        discord_id = int(row.get("discord_id") or 0)
        if discord_id > 0:
            owner_ids[position] = discord_id
    return owner_ids


def _compute_enabled_positions(*, roster_states: list[str], total_positions: int) -> tuple[set[int], set[int]]:
    enabled_start_positions: set[int] = set()
    enabled_end_positions: set[int] = set()
    in_progress_position = next(
        (i for i, state in enumerate(roster_states[:total_positions], start=1) if state == "in_progress"),
        None,
    )
    if in_progress_position is not None:
        enabled_end_positions = {in_progress_position}
        enabled_start_positions = set()
    else:
        next_waiting_position = next(
            (i for i, state in enumerate(roster_states[:total_positions], start=1) if state == "waiting"),
            None,
        )
        enabled_start_positions = {next_waiting_position} if next_waiting_position is not None else set()
        enabled_end_positions = set()
    return enabled_start_positions, enabled_end_positions




def _apply_energy_poll(*, saw_nonzero_energy: bool, consecutive_low_energy_polls: int, energy: int) -> tuple[bool, int, bool]:
    saw_nonzero = bool(saw_nonzero_energy)
    consecutive = int(consecutive_low_energy_polls)
    if int(energy) > 9:
        saw_nonzero = True
        consecutive = 0
    elif saw_nonzero and int(energy) < 10:
        consecutive += 1
    else:
        consecutive = 0
    should_finish = saw_nonzero and consecutive >= 4
    return saw_nonzero, consecutive, should_finish

def _visible_jump_state(state: str) -> str:
    normalized = str(state or "waiting").lower()
    if normalized == "in_progress":
        return "Jumping"
    if normalized == "done":
        return "Finished"
    if normalized in {"skipped", "removed"}:
        return normalized.title()
    return "Waiting"


def _format_jump_torn_identity(*, torn_name: str | None, torn_user_id: int | None, fallback_name: str) -> str:
    name = str(torn_name or "").strip()
    torn_id = int(torn_user_id or 0)
    fallback = str(fallback_name or "").strip() or "User"
    if name and torn_id > 0:
        return f"{name}[{torn_id}]"
    if name:
        return name
    if torn_id > 0:
        return f"{fallback}[{torn_id}]"
    return fallback




def _progress_bar(current: int | None, total: int | None, width: int = 10) -> str:
    if current is None or total is None or total <= 0:
        return f"[{'░' * width}]"
    ratio = max(0.0, min(float(current) / float(total), 1.0))
    filled = int(round(ratio * width))
    return f"[{'█' * filled}{'░' * (width - filled)}]"


def _format_duration_short(seconds: int | None) -> str:
    if seconds is None:
        return "Unknown"
    remaining = max(0, int(seconds))
    if remaining == 0:
        return "Ready"
    hours, rem = divmod(remaining, 3600)
    minutes = rem // 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m left"
    return f"{minutes}m left"


def _render_energy_bar(energy: int | None) -> str:
    if energy is None:
        return f"{_progress_bar(None, None)} Unknown"
    bounded = max(0, int(energy))
    return f"{_progress_bar(min(bounded, 1000), 1000)} {min(bounded, 1000)}/1000"


def _render_cooldown_bar(seconds: int | None, visual_cap_seconds: int = 28800) -> str:
    if seconds is None:
        return f"{_progress_bar(None, None)} Unknown"
    remaining = max(0, int(seconds))
    ready_progress = max(0.0, min(1.0, 1 - (min(remaining, visual_cap_seconds) / visual_cap_seconds)))
    filled = int(round(ready_progress * 10))
    bar = f"[{'█' * filled}{'░' * (10 - filled)}]"
    return f"{bar} {_format_duration_short(remaining)}"


def _classify_jump_readiness(
    *,
    energy: int | None,
    drug_cooldown: int | None,
    booster_cooldown: int | None,
    status_text: str | None,
    has_api_key: bool,
) -> str:
    status_lower = str(status_text or "").strip().lower()
    if not has_api_key:
        return "API Key Missing"
    if "permission" in status_lower or "bars/cooldowns" in status_lower:
        return "API Permissions Missing"
    if "unavailable" in status_lower or "rate limit" in status_lower or "timeout" in status_lower:
        return "API Unavailable"
    if energy is None or drug_cooldown is None or booster_cooldown is None:
        return "API Unavailable"

    blockers = 0
    if int(energy) < 1000:
        blockers += 1
    if int(drug_cooldown) > 0:
        blockers += 1
    if int(booster_cooldown) > 0:
        blockers += 1

    if blockers == 0:
        return "Ready Now"
    if blockers > 1:
        return "Waiting on Multiple"
    if int(energy) < 1000:
        return "Waiting on Energy"
    if int(drug_cooldown) > 0:
        return "Waiting on Drug CD"
    return "Waiting on Booster CD"


def _format_who_can_jump_identity(*, torn_name: str | None, torn_user_id: int | None, fallback_name: str) -> str:
    return _format_jump_torn_identity(
        torn_name=torn_name,
        torn_user_id=torn_user_id,
        fallback_name=(str(fallback_name or "").strip() or "Unknown member"),
    )


def _get_who_can_jump_manual_refresh_wait_seconds(guild_id: int) -> int:
    last_refresh = _WHO_CAN_JUMP_LAST_MANUAL_REFRESH.get(int(guild_id))
    if not isinstance(last_refresh, datetime):
        return 0
    remaining = _WHO_CAN_JUMP_MANUAL_REFRESH_COOLDOWN_SECONDS - (
        datetime.now(timezone.utc) - last_refresh
    ).total_seconds()
    return max(0, int(math.ceil(remaining)))


class WhoCanJumpPanelView(discord.ui.View):
    def __init__(self, guild_id: int, *, page_index: int, total_pages: int):
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self.page_index = max(0, int(page_index))
        self.total_pages = max(1, int(total_pages))

        prev_btn = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            custom_id=f"who_can_jump_prev:{self.guild_id}",
            row=0,
            disabled=self.page_index <= 0,
        )
        next_btn = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            custom_id=f"who_can_jump_next:{self.guild_id}",
            row=0,
            disabled=self.page_index >= self.total_pages - 1,
        )
        refresh_btn = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.primary,
            custom_id=f"who_can_jump_refresh:{self.guild_id}",
            row=0,
        )
        prev_btn.callback = self._on_prev
        next_btn.callback = self._on_next
        refresh_btn.callback = self._on_refresh
        self.add_item(prev_btn)
        self.add_item(next_btn)
        self.add_item(refresh_btn)

    async def _on_prev(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _refresh_who_can_jump_panel_for_guild(self.guild_id, requested_page_index=self.page_index - 1)

    async def _on_next(self, interaction: discord.Interaction):
        await interaction.response.defer()
        await _refresh_who_can_jump_panel_for_guild(self.guild_id, requested_page_index=self.page_index + 1)

    async def _on_refresh(self, interaction: discord.Interaction):
        guild_lock = _WHO_CAN_JUMP_REFRESH_LOCKS.setdefault(self.guild_id, asyncio.Lock())
        if guild_lock.locked():
            if interaction.response.is_done():
                await interaction.followup.send("A refresh is already in progress.", ephemeral=True)
            else:
                await interaction.response.send_message("A refresh is already in progress.", ephemeral=True)
            return

        wait_seconds = _get_who_can_jump_manual_refresh_wait_seconds(self.guild_id)
        if wait_seconds > 0:
            message = f"This panel was refreshed recently. Try again in {wait_seconds}s."
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

        _WHO_CAN_JUMP_LAST_MANUAL_REFRESH[self.guild_id] = datetime.now(timezone.utc)
        await interaction.response.defer()
        await _refresh_who_can_jump_panel_for_guild(self.guild_id, requested_page_index=self.page_index)


async def _collect_who_can_jump_rows(
    *, guild: discord.Guild, settings: dict, users_repo: UsersRepository
) -> tuple[list[dict], str | None]:
    host_role_id = int(settings.get("host99k_role_id") or 0)
    if host_role_id <= 0:
        return [], "setup_needed"

    role = guild.get_role(host_role_id)
    if role is None:
        return [], "setup_needed"

    rows: list[dict] = []
    for member in role.members:
        if member.bot:
            continue

        try:
            readiness = await _fetch_who_can_jump_readiness(
                users_repo=users_repo,
                discord_id=int(member.id),
                guild_id=int(guild.id),
            )
        except Exception:
            log.exception("Failed who-can-jump row guild_id=%s discord_id=%s", guild.id, member.id)
            readiness = {
                "has_api_key": True,
                "torn_name": None,
                "torn_user_id": None,
                "energy": None,
                "drug_cooldown": None,
                "booster_cooldown": None,
                "status_text": "API unavailable",
            }

        has_api_key = bool(readiness.get("has_api_key"))
        torn_name = str(readiness.get("torn_name") or "").strip() or None
        torn_user_id = int(readiness.get("torn_user_id") or 0) or None
        energy = readiness.get("energy")
        drug_cd = readiness.get("drug_cooldown")
        booster_cd = readiness.get("booster_cooldown")
        status_text = str(readiness.get("status_text") or "")

        status = _classify_jump_readiness(
            energy=energy,
            drug_cooldown=drug_cd,
            booster_cooldown=booster_cd,
            status_text=status_text,
            has_api_key=has_api_key,
        )
        rows.append(
            {
                "identity": _format_who_can_jump_identity(
                    torn_name=torn_name,
                    torn_user_id=torn_user_id,
                    fallback_name=str(member.display_name or "").strip() or "Unknown member",
                ),
                "status": status,
                "energy": energy if not status.startswith("API ") else None,
                "drug_cooldown": drug_cd if not status.startswith("API ") else None,
                "booster_cooldown": booster_cd if not status.startswith("API ") else None,
            }
        )

    status_rank = {
        "Ready Now": 0,
        "Waiting on Energy": 1,
        "Waiting on Drug CD": 1,
        "Waiting on Booster CD": 1,
        "Waiting on Multiple": 2,
        "API Key Missing": 3,
        "API Permissions Missing": 3,
        "API Unavailable": 3,
    }
    rows.sort(key=lambda r: (status_rank.get(r["status"], 9), str(r["identity"]).lower()))
    return rows, None


async def _fetch_who_can_jump_readiness(
    *,
    users_repo: UsersRepository,
    discord_id: int,
    guild_id: int,
) -> dict:
    key_row = await users_repo.get_user_api_key(int(discord_id))
    encrypted_key = (key_row or {}).get("encrypted_key") or (key_row or {}).get("api_key_encrypted")
    torn_name = str((key_row or {}).get("torn_name") or "").strip() or None
    torn_user_id = int((key_row or {}).get("torn_user_id") or 0) or None
    if not encrypted_key:
        return {
            "has_api_key": False,
            "torn_name": torn_name,
            "torn_user_id": torn_user_id,
            "energy": None,
            "energy_max": None,
            "drug_cooldown": None,
            "booster_cooldown": None,
            "status_text": "API key missing",
        }

    cache_key = (int(guild_id), int(discord_id))
    now = datetime.now(timezone.utc)
    cached = _WHO_CAN_JUMP_READINESS_CACHE.get(cache_key)
    if cached and (now - cached[0]).total_seconds() <= _WHO_CAN_JUMP_READINESS_CACHE_TTL_SECONDS:
        return dict(cached[1])

    try:
        api_key = get_security_manager().decrypt_api_key(encrypted_key)
        user_data = await get_torn_api().get_user_data(
            api_key,
            audit_discord_id=int(discord_id),
            audit_torn_id=torn_user_id,
            audit_context="who_can_jump",
            audit_query_meta={"guild_id": int(guild_id)},
        )
        profile = (user_data or {}).get("profile") or {}
        torn_name = str(profile.get("name") or torn_name or "").strip() or None
        torn_user_id = int(profile.get("id") or torn_user_id or 0) or None
        payload = {
            "has_api_key": True,
            "torn_name": torn_name,
            "torn_user_id": torn_user_id,
            "energy": int((user_data or {}).get("bars", {}).get("energy", {}).get("current", 0) or 0),
            "energy_max": int((user_data or {}).get("bars", {}).get("energy", {}).get("maximum", 0) or 0),
            "drug_cooldown": int((user_data or {}).get("cooldowns", {}).get("drug", 0) or 0),
            "booster_cooldown": int((user_data or {}).get("cooldowns", {}).get("booster", 0) or 0),
            "status_text": "ok",
        }
        _WHO_CAN_JUMP_READINESS_CACHE[cache_key] = (datetime.now(timezone.utc), payload)
        return payload
    except TornAPIPermissionError:
        return {
            "has_api_key": True,
            "torn_name": torn_name,
            "torn_user_id": torn_user_id,
            "energy": None,
            "energy_max": None,
            "drug_cooldown": None,
            "booster_cooldown": None,
            "status_text": "API permissions missing",
        }
    except (TornAPIRateLimitError, TornAPIError):
        return {
            "has_api_key": True,
            "torn_name": torn_name,
            "torn_user_id": torn_user_id,
            "energy": None,
            "energy_max": None,
            "drug_cooldown": None,
            "booster_cooldown": None,
            "status_text": "API unavailable",
        }
    except Exception:
        log.exception("Who-can-jump readiness fetch failed guild_id=%s discord_id=%s", guild_id, discord_id)
        return {
            "has_api_key": True,
            "torn_name": torn_name,
            "torn_user_id": torn_user_id,
            "energy": None,
            "energy_max": None,
            "drug_cooldown": None,
            "booster_cooldown": None,
            "status_text": "API unavailable",
        }


def _build_who_can_jump_embed(
    *,
    rows: list[dict],
    state: str | None,
    page_index: int,
    page_size: int = 10,
) -> tuple[discord.Embed, int, int]:
    now = discord.utils.utcnow()
    total_pages = max(1, (len(rows) + page_size - 1) // page_size)
    clamped_page_index = min(max(0, int(page_index)), total_pages - 1)

    ready = sum(1 for row in rows if row["status"] == "Ready Now")
    api_issues = sum(1 for row in rows if row["status"].startswith("API "))
    waiting = len(rows) - ready - api_issues

    summary = (
        f"Ready: {ready} • Waiting: {waiting} • API Issues: {api_issues}\n"
        f"Updated: {discord.utils.format_dt(now, style='R')}\n"
    )

    if state == "setup_needed":
        embed = create_info_embed(
            "Who Can Jump",
            summary + "\nSetup needed: configure a valid **99k_Jump_Host role** in `/setup`.",
        )
        embed.set_footer(text="Page 1/1")
        return embed, 1, 0

    if not rows:
        embed = create_info_embed(
            "Who Can Jump",
            summary + "\nNo non-bot members currently have the configured host role.",
        )
        embed.set_footer(text="Page 1/1")
        return embed, 1, 0

    start = clamped_page_index * page_size
    page_rows = rows[start : start + page_size]
    blocks: list[str] = []
    for row in page_rows:
        blocks.append(
            f"**{row['identity']}** • {row['status']}\n"
            f"⚡ Energy   {_render_energy_bar(row.get('energy'))}\n"
            f"💊 Drug CD  {_render_cooldown_bar(row.get('drug_cooldown'))}\n"
            f"🧃 Boost CD {_render_cooldown_bar(row.get('booster_cooldown'))}"
        )
    embed = create_info_embed("Who Can Jump", summary + "\n" + "\n\n".join(blocks))
    embed.set_footer(text=f"Page {clamped_page_index + 1}/{total_pages}")
    return embed, total_pages, clamped_page_index


async def _ensure_who_can_jump_panel_message(
    *, guild: discord.Guild, settings_repo: GuildSettingsRepository, settings: dict
) -> discord.Message | None:
    channel_id = int(settings.get("who_can_jump_channel_id") or 0)
    if channel_id <= 0:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return None
    if not hasattr(channel, "permissions_for") or not hasattr(channel, "fetch_message") or not hasattr(channel, "send"):
        return None

    bot_member = await _resolve_bot_member(guild)
    perms = channel.permissions_for(bot_member)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return None

    message_id = int(settings.get("who_can_jump_message_id") or 0)
    if message_id > 0:
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound:
            pass
        except Exception:
            return None

    sent = await channel.send(embed=create_info_embed("Who Can Jump", "Initializing panel..."))
    await settings_repo.upsert_settings(int(guild.id), who_can_jump_message_id=int(sent.id))
    settings["who_can_jump_message_id"] = int(sent.id)
    return sent


async def _refresh_who_can_jump_panel_for_guild(guild_id: int, *, requested_page_index: int | None = None) -> None:
    guild_id = int(guild_id)
    guild_lock = _WHO_CAN_JUMP_REFRESH_LOCKS.setdefault(guild_id, asyncio.Lock())
    if guild_lock.locked():
        return

    async with guild_lock:
        await _refresh_who_can_jump_panel_for_guild_locked(guild_id, requested_page_index=requested_page_index)


async def _refresh_who_can_jump_panel_for_guild_locked(guild_id: int, *, requested_page_index: int | None = None) -> None:
    guild = bot.get_guild(int(guild_id))
    if guild is None:
        return
    db = get_database()
    settings_repo = GuildSettingsRepository(db)
    settings = await settings_repo.get_settings(int(guild.id))
    if not settings.get("who_can_jump_channel_id"):
        return

    message = await _ensure_who_can_jump_panel_message(guild=guild, settings_repo=settings_repo, settings=settings)
    if message is None:
        return

    users_repo = UsersRepository(db.pool)
    rows, state = await _collect_who_can_jump_rows(
        guild=guild,
        settings=settings,
        users_repo=users_repo,
    )

    stored_page_index = int(settings.get("who_can_jump_page_index") or 0)
    page_index = stored_page_index if requested_page_index is None else int(requested_page_index)
    embed, total_pages, clamped_page_index = _build_who_can_jump_embed(
        rows=rows,
        state=state,
        page_index=page_index,
        page_size=10,
    )
    view = WhoCanJumpPanelView(int(guild.id), page_index=clamped_page_index, total_pages=total_pages)

    updates: dict[str, int | None] = {}
    if int(settings.get("who_can_jump_page_index") or 0) != clamped_page_index:
        updates["who_can_jump_page_index"] = clamped_page_index
    if updates:
        await settings_repo.upsert_settings(int(guild.id), **updates)
        settings.update(updates)

    render_signature = json.dumps(
        {
            "state": state,
            "rows": rows,
            "page": clamped_page_index,
            "total_pages": total_pages,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    now = datetime.now(timezone.utc)
    previous_render = _WHO_CAN_JUMP_LAST_RENDER.get(int(guild.id), {})
    if (
        previous_render.get("signature") == render_signature
        and isinstance(previous_render.get("edited_at"), datetime)
        and (now - previous_render["edited_at"]).total_seconds() < 10
    ):
        return

    async def _clear_panel_ref() -> None:
        await settings_repo.upsert_settings(int(guild.id), who_can_jump_message_id=None)
        settings["who_can_jump_message_id"] = None

    try:
        edited = await PANEL_EDIT_SAFETY.request_edit(
            message,
            content=None,
            embed=embed,
            view=view,
            min_interval_seconds=20,
            force=bool(requested_page_index is not None),
            not_found_cb=_clear_panel_ref,
        )
        if edited:
            _WHO_CAN_JUMP_LAST_RENDER[int(guild.id)] = {"signature": render_signature, "edited_at": now}
            return
        if settings.get("who_can_jump_message_id") is None:
            retry = await _ensure_who_can_jump_panel_message(guild=guild, settings_repo=settings_repo, settings=settings)
            if retry is not None:
                retried = await PANEL_EDIT_SAFETY.request_edit(
                    retry,
                    content=None,
                    embed=embed,
                    view=view,
                    min_interval_seconds=20,
                    force=True,
                )
                if retried:
                    _WHO_CAN_JUMP_LAST_RENDER[int(guild.id)] = {"signature": render_signature, "edited_at": now}
        return
    except Exception:
        log.exception("Failed safe-editing who-can-jump panel guild_id=%s", guild.id)
        return


async def register_persistent_who_can_jump_views() -> None:
    db = get_database()
    repo = GuildSettingsRepository(db)
    async with acquire_conn(db.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            """
            SELECT guild_id, who_can_jump_message_id, COALESCE(who_can_jump_page_index, 0) AS who_can_jump_page_index
            FROM public.guild_settings
            WHERE who_can_jump_channel_id IS NOT NULL
              AND who_can_jump_message_id IS NOT NULL
            """
        )
    for row in rows:
        guild_id = int(row.get("guild_id") or 0)
        message_id = int(row.get("who_can_jump_message_id") or 0)
        page_index = int(row.get("who_can_jump_page_index") or 0)
        if guild_id <= 0 or message_id <= 0:
            continue
        bot.add_view(WhoCanJumpPanelView(guild_id, page_index=page_index, total_pages=1), message_id=message_id)
        try:
            await _refresh_who_can_jump_panel_for_guild(guild_id)
        except Exception:
            log.exception("Failed initial who-can-jump refresh guild_id=%s", guild_id)


@tasks.loop(seconds=60)
async def who_can_jump_panel_worker():
    if not await _worker_db_ready("who_can_jump_panel_worker"):
        return
    db = get_database()
    async with acquire_conn(db.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
        rows = await conn.fetch(
            "SELECT guild_id FROM public.guild_settings WHERE who_can_jump_channel_id IS NOT NULL"
        )
    for row in rows:
        guild_id = int(row.get("guild_id") or 0)
        if guild_id <= 0:
            continue
        try:
            await _refresh_who_can_jump_panel_for_guild(guild_id)
        except Exception:
            log.exception("who_can_jump_panel_worker guild refresh failed guild_id=%s", guild_id)


@who_can_jump_panel_worker.before_loop
async def before_who_can_jump_panel_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("who_can_jump_panel_worker")


async def _build_jump_transition_notification(
    *,
    users_repo: UsersRepository,
    session: dict,
    roster_rows: list[dict],
    previous_discord_id: int,
    next_discord_id: int,
    guild: discord.Guild | None = None,
) -> str:
    roster_by_discord_id: dict[int, dict] = {
        int(row.get("discord_id") or 0): row
        for row in roster_rows
    }
    identity_cache: dict[int, str] = {}

    async def _resolve_torn_identity(discord_id: int) -> str:
        resolved = identity_cache.get(int(discord_id))
        if resolved is not None:
            return resolved

        roster_row = roster_by_discord_id.get(int(discord_id), {})
        torn_name = str(roster_row.get("participant_torn_name") or "").strip() or None
        torn_user_id = int(roster_row.get("participant_torn_user_id") or roster_row.get("torn_user_id") or 0)

        if not torn_name or torn_user_id <= 0:
            user_row = await users_repo.get_user_api_key(int(discord_id))
            if user_row:
                if not torn_name:
                    torn_name = str(user_row.get("torn_name") or "").strip() or None
                if torn_user_id <= 0:
                    torn_user_id = int(user_row.get("torn_user_id") or 0)

        human_fallback_name = f"<@{int(discord_id)}>"
        if guild is not None:
            member = guild.get_member(int(discord_id))
            if member is None:
                try:
                    member = await guild.fetch_member(int(discord_id))
                except Exception:
                    member = None
            if member is not None:
                human_fallback_name = str(member.display_name or "").strip() or human_fallback_name
        if not human_fallback_name:
            human_fallback_name = "Unknown user"

        identity = _format_jump_torn_identity(
            torn_name=torn_name,
            torn_user_id=torn_user_id if torn_user_id > 0 else None,
            fallback_name=human_fallback_name,
        )
        identity_cache[int(discord_id)] = identity
        return identity

    previous_identity = await _resolve_torn_identity(int(previous_discord_id))
    next_identity = await _resolve_torn_identity(int(next_discord_id))
    return (
        f"{previous_identity} is finished, <@{int(next_discord_id)}> {next_identity} "
        f"use poison mistletoe on {previous_identity} and begin your jump"
    )


async def _load_automation_roster(repo: JumpsRepository, session_id: int) -> tuple[dict, list[dict], list[dict]]:
    session = await repo.get_session(session_id)
    signups = await repo.list_roster_signups_with_readiness(session_id)
    progress = await repo.get_jump_progress(session_id)
    host_id = int((session or {}).get("host_discord_id") or 0)
    participants = [row for row in signups if int(row.get("discord_id") or 0) != host_id]
    progress_signups = progress.get("signups") or []
    return session, participants, progress_signups


async def _advance_to_next_jumper(*, repo: JumpsRepository, session_id: int, actor_discord_id: int) -> tuple[bool, int | None]:
    session, participants, progress_signups = await _load_automation_roster(repo, session_id)
    if not session:
        return False, None
    host_state = str((await repo.get_jump_progress(session_id)).get("host", {}).get("state") or "waiting")
    if host_state == "waiting":
        ok, _ = await repo.run_jump_transition_by_position(session_id=session_id, position=1, action="start", actor_discord_id=actor_discord_id)
        return ok, int(session.get("host_discord_id") or 0) if ok else None

    for idx, row in enumerate(participants, start=2):
        if idx - 2 < len(progress_signups):
            state = str(progress_signups[idx - 2].get("state") or "waiting")
        else:
            state = "waiting"
        if state == "waiting":
            ok, _ = await repo.run_jump_transition_by_position(session_id=session_id, position=idx, action="start", actor_discord_id=actor_discord_id)
            return ok, int(row.get("discord_id") or 0) if ok else None
    return False, None


async def _build_jump99k_manage_view(session_id: int) -> discord.ui.View:
    return Jump99kHostControlsView(int(session_id))


def is_host_override(member: discord.Member, session: dict, host_role_id: int | None) -> bool:
    if int(member.id) == int(session.get("host_discord_id") or 0):
        return True
    if member.guild_permissions.administrator:
        return True
    if host_role_id and any(int(role.id) == int(host_role_id) for role in member.roles):
        return True
    return False


def can_user_press_position(
    member: discord.Member,
    *,
    session: dict,
    position: int,
    position_owner_ids: dict[int, int],
    host_override: bool,
) -> tuple[bool, str]:
    if host_override:
        return True, ""
    if position not in position_owner_ids:
        return False, "That slot is empty."
    if int(member.id) != int(position_owner_ids[position]):
        return False, "Only the assigned jumper for this slot can press this button."
    return True, ""


async def _refresh_roster_panel(session_id: int, channel: discord.abc.Messageable, message: discord.Message | None = None) -> tuple[discord.Embed, str]:
    embed, view = await build_roster_panel(session_id, channel)
    roster_text = embed.description or ""

    if message is not None:
        await PANEL_EDIT_SAFETY.request_edit(
            message,
            embed=embed,
            view=view,
            min_interval_seconds=10,
            force=False,
        )

    repo = JumpsRepository(get_pool())
    await repo.touch_roster_refreshed(session_id)
    return embed, roster_text


async def _refresh_stored_roster_panel_message(bot_client: commands.Bot, session_id: int, *, session: dict | None = None) -> str:
    repo = JumpsRepository(get_pool())
    if session is None:
        session = await repo.get_session(int(session_id))
    if not session:
        return "missing_session"

    roster_channel_id = session.get("roster_channel_id") or session.get("private_channel_id")
    roster_message_id = session.get("roster_message_id")
    if not roster_channel_id or not roster_message_id:
        return "missing_reference"

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
        return "refreshed"
    except (discord.NotFound, discord.Forbidden):
        await repo.clear_roster_panel_message(int(session_id))
        return "missing_message"
    except (aiohttp.ClientOSError, aiohttp.ClientConnectionError, ConnectionResetError) as exc:
        log.warning(
            "Roster refresh transient network error session=%s guild_id=%s channel_id=%s message_id=%s error_type=%s",
            session_id,
            session.get("guild_id"),
            roster_channel_id,
            roster_message_id,
            type(exc).__name__,
        )
        return "error"
    except discord.HTTPException:
        log.warning("Roster refresh HTTPException for session=%s", session_id)
        return "error"
    except Exception:
        log.exception("Failed to refresh roster panel for session=%s", session_id)
        return "error"


async def _refresh_roster_if_exists(bot_client: commands.Bot, session_id: int) -> None:
    await _refresh_stored_roster_panel_message(bot_client, int(session_id))


async def _refresh_or_repost_roster_panel(bot_client: commands.Bot, session_id: int) -> bool:
    repo = JumpsRepository(get_pool())
    session = await repo.get_session(int(session_id))
    if not session:
        return False

    refresh_status = await _refresh_stored_roster_panel_message(bot_client, int(session_id), session=session)
    if refresh_status == "refreshed":
        return True
    if refresh_status == "error":
        return False

    private_channel_id = session.get("private_channel_id")
    if not private_channel_id:
        return False

    try:
        channel = bot_client.get_channel(int(private_channel_id)) or await bot_client.fetch_channel(int(private_channel_id))
        panel_embed, panel_view = await build_roster_panel(int(session_id), channel)
        roster_msg = await channel.send(embed=panel_embed, view=panel_view)
        await repo.set_roster_panel_message(
            int(session_id),
            channel_id=int(channel.id),
            message_id=int(roster_msg.id),
        )
        await repo.touch_roster_refreshed(int(session_id))
        return True
    except Exception:
        log.exception("Failed to re-post roster panel for session=%s", session_id)
        return False


async def _session_jump_started(repo: JumpsRepository, session_id: int) -> bool:
    state = _automation_state(int(session_id))
    if bool(state.get("running")):
        return True
    progress = await repo.get_jump_progress(int(session_id))
    host = progress.get("host") or {}
    host_state = str(host.get("state") or "waiting")
    if host.get("started_at") or host_state in {"in_progress", "done"}:
        return True
    for row in progress.get("signups") or []:
        row_state = str(row.get("state") or "waiting")
        if row.get("started_at") or row_state in {"in_progress", "done"}:
            return True
    return False


async def _safe_defer_ephemeral(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=False)
    except discord.InteractionResponded:
        return


async def _safe_edit_original(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    payload = {"content": content, "embed": embed, "view": view}
    if interaction.response.is_done():
        try:
            await interaction.edit_original_response(**payload)
            return
        except (discord.NotFound, discord.HTTPException):
            await interaction.followup.send(**payload, ephemeral=True)
            return
    await interaction.response.send_message(**payload, ephemeral=True)


class Jump99kRosterPanelView(discord.ui.View):
    MAX_POSITIONS = 8

    def __init__(self, session_id: int, *, roster_size: int | None = None):
        super().__init__(timeout=None)
        self.session_id = int(session_id)
        if roster_size is None:
            roster_size = self.MAX_POSITIONS
        self.roster_size = max(1, min(int(roster_size), self.MAX_POSITIONS))

        refresh_btn = discord.ui.Button(
            label="Refresh roster",
            style=discord.ButtonStyle.primary,
            custom_id=f"99k_roster_refresh:{self.session_id}",
            row=0,
        )
        host_controls_btn = discord.ui.Button(
            label="Host Controls",
            style=discord.ButtonStyle.primary,
            custom_id=f"99k_roster_host_controls:{self.session_id}",
            row=0,
        )
        refresh_btn.callback = self._on_refresh
        host_controls_btn.callback = self._on_host_controls
        self.add_item(refresh_btn)
        self.add_item(host_controls_btn)

    async def _on_host_controls(self, interaction: discord.Interaction):
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session:
            if interaction.response.is_done():
                await interaction.followup.send("Session not found.", ephemeral=True)
            else:
                await interaction.response.send_message("Session not found.", ephemeral=True)
            return
        if int(interaction.user.id) != int(session.get("host_discord_id") or 0):
            if interaction.response.is_done():
                await interaction.followup.send("Only the jump host can use these controls.", ephemeral=True)
            else:
                await interaction.response.send_message("Only the jump host can use these controls.", ephemeral=True)
            return
        if interaction.response.is_done():
            await interaction.followup.send(
                content="Host controls:",
                view=Jump99kHostControlsView(session_id=self.session_id),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                content="Host controls:",
                view=Jump99kHostControlsView(session_id=self.session_id),
                ephemeral=True,
            )

    async def _on_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session:
            await interaction.followup.send("Session not found.", ephemeral=True)
            return

        refreshed = False
        if interaction.client:
            refreshed = await _refresh_or_repost_roster_panel(interaction.client, self.session_id)
            await _refresh_99k_panel(interaction.client, self.session_id)

        if refreshed:
            await interaction.followup.send("✅ Roster refreshed.", ephemeral=True)
        else:
            await interaction.followup.send("Roster refresh queued.", ephemeral=True)


async def _end_99k_session_via_shared_flow(
    interaction: discord.Interaction,
    *,
    session: dict,
    actor_discord_id: int,
) -> tuple[bool, list[str]]:
    repo = JumpsRepository(get_pool())

    if session.get("status") == "open":
        rows = await repo.list_signups(int(session["id"]))
        completed_ids = [int(r["discord_id"]) for r in rows if r.get("status") == "paid"]
        ok = await repo.close_session_and_record(
            session_id=int(session["id"]),
            guild_id=int(interaction.guild_id),
            completed_discord_ids=completed_ids,
            not_completed_discord_ids=[],
        )
        if not ok:
            return False, [f"Could not close session #{int(session['id'])}."]
        if interaction.client:
            closed_at = datetime.now(timezone.utc)
            for completed_user_id in completed_ids:
                interaction.client.dispatch(
                    "jump_99k_completed",
                    {
                        "guild_id": int(session.get("guild_id") or interaction.guild_id or 0),
                        "user_id": int(completed_user_id),
                        "session_id": int(session.get("id") or 0),
                        "host_user_id": int(session.get("host_discord_id") or 0) if session.get("host_discord_id") is not None else None,
                        "closed_at": closed_at,
                        "dedupe_key": f"jump_completed:{int(session.get('id') or 0)}:{int(completed_user_id)}",
                    },
                )

    perm_report = validate_99k_permissions(
        interaction.guild,
        bot.user,
        signup_channel_id=int(session.get("announce_channel_id") or 0) or None,
        announce_channel_id=int(session.get("announce_channel_id") or 0) or None,
        private_category_id=int(session.get("private_channel_id") or 0) or None,
    )
    missing_lines = []
    for cname, ch in perm_report.get("channels", {}).items():
        missing = ch.get("missing_permissions", [])
        if missing:
            missing_lines.append(f"{cname}: {', '.join(missing)}")

    if interaction.client:
        await _refresh_roster_if_exists(interaction.client, int(session["id"]))
        await _disable_99k_session_messages(interaction.client, session, status_text="Session closed")

    def _result_line(label: str, result: tuple[bool, str]) -> str:
        success, reason = result
        if success:
            return f"{label}: ✅"
        return f"{label}: ❌ ({reason})"

    session_id = int(session["id"])
    announce_channel_id, announce_message_id = get_announce_ids(session)
    private_channel_id = int(session["private_channel_id"]) if session.get("private_channel_id") else None
    roster_channel_id = int(session["roster_channel_id"]) if session.get("roster_channel_id") else None
    roster_message_id = int(session["roster_message_id"]) if session.get("roster_message_id") else None
    host_controls_channel_id = int(session["host_controls_channel_id"]) if session.get("host_controls_channel_id") else None
    host_controls_message_id = int(session["host_controls_message_id"]) if session.get("host_controls_message_id") else None

    signup_panel_result = await _delete_99k_signup_panel_with_fallback(
        guild=interaction.guild,
        bot_user_id=int(interaction.client.user.id) if interaction.client and interaction.client.user else None,
        session_id=session_id,
        announce_channel_id=announce_channel_id,
        announce_message_id=announce_message_id,
        reason=f"99k session {session_id} ended",
    )
    roster_panel_channel_id = roster_channel_id or private_channel_id
    roster_result = await delete_message_safe(interaction.guild, roster_panel_channel_id, roster_message_id, f"99k session {session_id} ended", {"session_id": session_id})
    host_controls_result = await delete_message_safe(interaction.guild, host_controls_channel_id, host_controls_message_id, f"99k session {session_id} ended", {"session_id": session_id})
    channel_result = await delete_channel_safe(interaction.guild, private_channel_id, f"99k session {session_id} ended", {"session_id": session_id})

    if signup_panel_result[0]:
        await repo.clear_announcement_message(session_id)
    if roster_result[0]:
        await repo.clear_roster_panel_message(session_id)
    if host_controls_result[0]:
        await repo.clear_host_controls_message(session_id)
    if channel_result[0]:
        await repo.clear_private_channel_only(session_id)

    cleanup_complete = signup_panel_result[0] and roster_result[0] and host_controls_result[0] and channel_result[0]
    if not signup_panel_result[0]:
        await repo.enqueue_cleanup_task(guild_id=int(interaction.guild_id), session_id=session_id, task_type="delete_message", channel_id=announce_channel_id, message_id=announce_message_id, error=signup_panel_result[1])
    if not roster_result[0]:
        await repo.enqueue_cleanup_task(guild_id=int(interaction.guild_id), session_id=session_id, task_type="delete_message", channel_id=roster_panel_channel_id, message_id=roster_message_id, error=roster_result[1])
    if not host_controls_result[0]:
        await repo.enqueue_cleanup_task(guild_id=int(interaction.guild_id), session_id=session_id, task_type="delete_message", channel_id=host_controls_channel_id, message_id=host_controls_message_id, error=host_controls_result[1])
    if not channel_result[0]:
        await repo.enqueue_cleanup_task(guild_id=int(interaction.guild_id), session_id=session_id, task_type="delete_channel", channel_id=private_channel_id, message_id=None, error=channel_result[1])
    if cleanup_complete:
        await repo.mark_cleaned(session_id)

    summary_lines = [
        f"Ended session #{session['id']}.",
        _result_line("Signup panel removed", signup_panel_result),
        _result_line("Roster panel removed", roster_result),
        _result_line("Host controls removed", host_controls_result),
        _result_line("Private channel deleted", channel_result),
    ]
    if not cleanup_complete:
        summary_lines.append("Cleanup incomplete; fix permissions and run /99k end again to retry.")
    if missing_lines:
        summary_lines.append("Permission warnings:\n" + "\n".join(missing_lines))

    log_event(log, logging.INFO, "jump99k.end", guild_id=interaction.guild_id, session_id=session_id, user_id=actor_discord_id, action="end", result="ok" if cleanup_complete else "partial")
    return True, summary_lines


async def _delete_99k_signup_panel_with_fallback(
    *,
    guild: discord.Guild,
    bot_user_id: int | None,
    session_id: int,
    announce_channel_id: int | None,
    announce_message_id: int | None,
    reason: str,
) -> tuple[bool, str]:
    result = await delete_message_safe(
        guild,
        announce_channel_id,
        announce_message_id,
        reason,
        {"session_id": session_id},
    )

    should_fallback_lookup = announce_channel_id and announce_message_id and result[1] == "already_deleted"
    if not should_fallback_lookup:
        return result

    try:
        channel = guild.get_channel(int(announce_channel_id)) or await guild.fetch_channel(int(announce_channel_id))
    except Exception:
        return result

    if not hasattr(channel, "history"):
        return result

    marker = f"Session #{int(session_id)}"
    try:
        async for msg in channel.history(limit=50):
            if bot_user_id and int(getattr(getattr(msg, "author", None), "id", 0) or 0) != int(bot_user_id):
                continue
            content = str(getattr(msg, "content", "") or "")
            if "99k Happy Jump" not in content or marker not in content:
                continue
            try:
                await msg.delete(reason=reason)
            except TypeError:
                await msg.delete()
            return True, "ok"
    except (discord.Forbidden, discord.HTTPException):
        return False, "fallback_lookup_failed"
    except Exception:
        return result

    return result

async def _grant_private_channel_access(
    guild: discord.Guild | None, session: dict, discord_id: int
) -> bool:
    if guild is None:
        return False
    private_channel_id = session.get("private_channel_id")
    if not private_channel_id:
        return False

    member = guild.get_member(int(discord_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(discord_id))
        except Exception:
            log.warning(
                "Private channel access grant failed while fetching member session_id=%s guild_id=%s discord_id=%s private_channel_id=%s",
                session.get("id"),
                guild.id,
                discord_id,
                private_channel_id,
                exc_info=True,
            )
            member = None
    if member is None:
        return False

    try:
        channel = guild.get_channel(int(private_channel_id)) or await guild.fetch_channel(int(private_channel_id))
    except Exception:
        log.warning(
            "Private channel access grant failed while fetching channel session_id=%s guild_id=%s discord_id=%s private_channel_id=%s",
            session.get("id"),
            guild.id,
            discord_id,
            private_channel_id,
            exc_info=True,
        )
        return False
    if isinstance(channel, discord.abc.GuildChannel):
        try:
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
            return True
        except Exception:
            log.warning(
                "Private channel access grant failed while setting permissions session_id=%s guild_id=%s discord_id=%s private_channel_id=%s",
                session.get("id"),
                guild.id,
                discord_id,
                private_channel_id,
                exc_info=True,
            )
            return False
    return False


async def _revoke_private_channel_access(guild: discord.Guild | None, session: dict, discord_id: int) -> None:
    if guild is None:
        return
    private_channel_id = session.get("private_channel_id")
    if not private_channel_id:
        return

    member = guild.get_member(int(discord_id))
    if member is None:
        try:
            member = await guild.fetch_member(int(discord_id))
        except Exception:
            member = None
    if member is None:
        return

    try:
        channel = guild.get_channel(int(private_channel_id)) or await guild.fetch_channel(int(private_channel_id))
    except Exception:
        return
    if isinstance(channel, discord.abc.GuildChannel):
        await channel.set_permissions(member, overwrite=None)


async def _list_removable_signups(*, repo: JumpsRepository, session: dict) -> list[dict]:
    session_id = int(session.get("id") or 0)
    host_discord_id = int(session.get("host_discord_id") or 0)
    progress = await repo.get_jump_progress(session_id)
    progress_by_discord: dict[int, str] = {
        int(row.get("discord_id") or 0): str(row.get("state") or "waiting")
        for row in (progress.get("signups") or [])
    }
    roster_signups = await repo.list_roster_signups_with_readiness(session_id)
    removable: list[dict] = []
    for row in roster_signups:
        discord_id = int(row.get("discord_id") or 0)
        if discord_id <= 0 or discord_id == host_discord_id:
            continue
        state = str(progress_by_discord.get(discord_id, "waiting") or "waiting").lower()
        if state in {"in_progress", "done", "removed"}:
            continue
        status = str(row.get("status") or "").lower()
        if status not in SIGNUP_ACTIVE_STATUSES:
            continue
        removable.append(dict(row))
    return removable


def _removable_signup_option_label(row: dict) -> tuple[str, str]:
    torn_name = str(row.get("participant_torn_name") or "").strip() or None
    torn_user_id = int(row.get("participant_torn_user_id") or row.get("torn_user_id") or 0)
    display_name = str(row.get("display_name") or "").strip() or None
    discord_id = int(row.get("discord_id") or 0)
    fallback_name = display_name or (f"<@{discord_id}>" if discord_id > 0 else "Unknown user")
    base_name = _format_jump_torn_identity(
        torn_name=torn_name,
        torn_user_id=torn_user_id if torn_user_id > 0 else None,
        fallback_name=fallback_name,
    )
    if bool(row.get("is_priority")):
        base_name = f"{base_name} [Priority]"
    status = str(row.get("status") or "paid").lower()
    description = f"Status: {status}"
    return base_name[:100], description[:100]


class Jump99kManualAddPickerView(discord.ui.View):
    def __init__(self, *, session_id: int):
        super().__init__(timeout=120)
        self.session_id = int(session_id)
        self.user_select = discord.ui.UserSelect(
            custom_id=f"99k_manual_add_select:{self.session_id}",
            placeholder="Pick a user…",
            min_values=1,
            max_values=1,
        )
        self.user_select.callback = self._on_select_user
        self.add_item(self.user_select)

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"99k_manual_add_cancel:{self.session_id}",
        )
        cancel_button.callback = self._on_cancel
        self.add_item(cancel_button)

    def _disable_controls(self) -> None:
        for item in self.children:
            item.disabled = True

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not await _can_use_manual_add_controls(interaction, session):
            await interaction.followup.send("You do not have permission.", ephemeral=True)
            return
        self._disable_controls()
        await _safe_edit_original(interaction, content="Cancelled.", view=self)

    async def _on_select_user(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        try:
            repo = JumpsRepository(get_pool())
            users_repo = UsersRepository(get_pool())
            session = await repo.get_session(self.session_id)
            if not await _can_use_manual_add_controls(interaction, session):
                await interaction.followup.send("You do not have permission.", ephemeral=True)
                return
            if not session or str(session.get("status", "")).lower() != "open":
                await interaction.followup.send("Session is not open.", ephemeral=True)
                return

            selected = self.user_select.values[0] if self.user_select.values else None
            if not selected:
                await interaction.followup.send("Please select a user.", ephemeral=True)
                return

            user_row = await users_repo.get_user_api_key(int(selected.id))
            torn_user_id = None
            torn_name = None
            if user_row:
                torn_user_id = int(user_row.get("torn_user_id") or 0) or None
                torn_name = str(user_row.get("torn_name") or "").strip() or None

            ok, msg = await repo.manual_add_as_verified_signup(
                session_id=self.session_id,
                guild_id=int(interaction.guild_id or 0),
                user_discord_id=int(selected.id),
                added_by_discord_id=int(interaction.user.id),
                torn_user_id=torn_user_id,
                torn_name=torn_name,
                reason=None,
            )
            if not ok:
                await interaction.followup.send(msg, ephemeral=True)
                return
        except Exception:
            log.exception(
                "Manual add select failed session_id=%s user_id=%s",
                self.session_id,
                interaction.user.id if interaction.user else None,
            )
            self._disable_controls()
            await _safe_edit_original(
                interaction,
                content="Sorry—could not add that user. Please try again.",
                view=self,
            )
            return

        had_post_add_failures = False

        if interaction.guild:
            try:
                await _grant_private_channel_access(interaction.guild, session, int(selected.id))
            except Exception:
                had_post_add_failures = True
                log.exception(
                    "Manual add channel access grant failed session_id=%s user_id=%s",
                    self.session_id,
                    selected.id,
                )

        missing_api_key = not bool((user_row or {}).get("encrypted_key") or (user_row or {}).get("api_key_encrypted"))
        if missing_api_key:
            private_channel_id = session.get("private_channel_id")
            private_channel = None
            if interaction.client and private_channel_id:
                try:
                    private_channel = interaction.client.get_channel(int(private_channel_id)) or await interaction.client.fetch_channel(int(private_channel_id))
                except Exception:
                    private_channel = None
            if private_channel:
                try:
                    await private_channel.send(
                        f"{selected.mention} please run /set_api_key so the bot can poll your energy/cooldowns for readiness."
                    )
                except Exception:
                    had_post_add_failures = True
                    log.exception("Manual add private reminder failed session_id=%s user_id=%s", self.session_id, selected.id)

            try:
                await selected.send(
                    "You were manually added to a 99k session. Please run /set_api_key so the bot can poll your energy/cooldowns for readiness."
                )
            except discord.Forbidden:
                log.debug("Manual add API key reminder DM blocked session_id=%s user_id=%s", self.session_id, selected.id)
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add DM failed session_id=%s user_id=%s", self.session_id, selected.id)
        else:
            try:
                await _fetch_and_upsert_user_readiness_snapshot(
                    repo=repo,
                    users_repo=users_repo,
                    session_id=self.session_id,
                    guild_id=int(session.get("guild_id") or 0),
                    discord_id=int(selected.id),
                )
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add readiness refresh failed session_id=%s user_id=%s", self.session_id, selected.id)

        roster_channel_id = session.get("roster_channel_id") or session.get("private_channel_id")
        roster_message_id = session.get("roster_message_id")
        if roster_channel_id and roster_message_id and interaction.client:
            try:
                channel = interaction.client.get_channel(int(roster_channel_id)) or await interaction.client.fetch_channel(int(roster_channel_id))
                message = await channel.fetch_message(int(roster_message_id))
                embed, view = await build_roster_panel(self.session_id, channel)
                await message.edit(embed=embed, view=view)
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add roster refresh failed session_id=%s user_id=%s", self.session_id, selected.id)

        if interaction.client:
            try:
                await _refresh_99k_panel(interaction.client, self.session_id)
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add panel refresh failed session_id=%s user_id=%s", self.session_id, selected.id)
            try:
                await _refresh_or_repost_roster_panel(interaction.client, self.session_id)
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add roster refresh failed session_id=%s user_id=%s", self.session_id, selected.id)

        self._disable_controls()
        success_message = f"✅ Added {selected.mention} to the jump."
        if had_post_add_failures:
            success_message += " Added user, but some follow-up updates could not be completed."
        await _safe_edit_original(interaction, content=success_message, view=self)
        if missing_api_key:
            try:
                await interaction.followup.send(
                    f"Added {selected.mention}, but their Energy/Cooldowns will not populate until they set an API key.",
                    ephemeral=True,
                )
            except Exception:
                had_post_add_failures = True
                log.exception("Manual add private reminder failed session_id=%s user_id=%s", self.session_id, selected.id)


class Jump99kManageJumpersActionView(discord.ui.View):
    def __init__(self, *, session_id: int):
        super().__init__(timeout=120)
        self.session_id = int(session_id)

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Add jumper", style=discord.ButtonStyle.primary)
    async def add_jumper(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _safe_defer_ephemeral(interaction)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session or not await can_manage_99k_session(interaction, session):
            self._disable_controls()
            await _safe_edit_original(interaction, content="You are not allowed to manage this session.", view=self)
            return
        self._disable_controls()
        await _safe_edit_original(interaction, content="Select a user to add to this jump:", view=Jump99kManualAddPickerView(session_id=self.session_id))

    @discord.ui.button(label="Remove jumper", style=discord.ButtonStyle.danger)
    async def remove_jumper(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _safe_defer_ephemeral(interaction)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session or not await can_manage_99k_session(interaction, session):
            self._disable_controls()
            await _safe_edit_original(interaction, content="You are not allowed to manage this session.", view=self)
            return

        removable = await _list_removable_signups(repo=repo, session=session)
        self._disable_controls()
        if not removable:
            await _safe_edit_original(interaction, content="No removable jumpers in the active roster.", view=self)
            return
        await _safe_edit_original(
            interaction,
            content="Select a jumper to remove from this jump:",
            view=Jump99kManualRemovePickerView(session_id=self.session_id, removable_signups=removable),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await _safe_defer_ephemeral(interaction)
        self._disable_controls()
        await _safe_edit_original(interaction, content="Cancelled.", view=self)


class Jump99kManualRemovePickerView(discord.ui.View):
    def __init__(self, *, session_id: int, removable_signups: list[dict]):
        super().__init__(timeout=120)
        self.session_id = int(session_id)
        self._options = removable_signups

        options = []
        for row in removable_signups[:25]:
            label, description = _removable_signup_option_label(row)
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=str(int(row.get("discord_id") or 0)),
                )
            )

        self.select = discord.ui.Select(
            custom_id=f"99k_manual_remove_select:{self.session_id}",
            placeholder="Pick a jumper to remove…",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            custom_id=f"99k_manual_remove_cancel:{self.session_id}",
        )
        cancel_button.callback = self._on_cancel
        self.add_item(cancel_button)

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        self._disable_controls()
        await _safe_edit_original(interaction, content="Cancelled.", view=self)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session or not await can_manage_99k_session(interaction, session):
            self._disable_controls()
            await _safe_edit_original(interaction, content="You are not allowed to manage this session.", view=self)
            return

        selected_value = self.select.values[0] if self.select.values else None
        if not selected_value:
            await interaction.followup.send("Please select a user to remove.", ephemeral=True)
            return

        removed_discord_id = int(selected_value)
        ok, message = await repo.manual_remove_signup(
            session_id=self.session_id,
            removed_discord_id=removed_discord_id,
            removed_by_discord_id=int(interaction.user.id),
        )
        if not ok:
            self._disable_controls()
            await _safe_edit_original(interaction, content=f"❌ {message}", view=self)
            return

        if interaction.client:
            await _refresh_99k_panel(interaction.client, self.session_id)
            await _refresh_or_repost_roster_panel(interaction.client, self.session_id)

        try:
            await _revoke_private_channel_access(interaction.guild, session, removed_discord_id)
        except Exception:
            log.exception("Manual remove private-channel revoke failed session_id=%s user_id=%s", self.session_id, removed_discord_id)

        log_event(
            log,
            logging.INFO,
            "jump99k.manual_remove",
            guild_id=interaction.guild_id,
            session_id=self.session_id,
            actor_id=int(interaction.user.id),
            removed_user_id=removed_discord_id,
            action="manual_remove",
            result="ok",
        )

        self._disable_controls()
        await _safe_edit_original(interaction, content=f"✅ {message}", view=self)


class Jump99kHostControlsView(discord.ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=None)
        self.session_id = int(session_id)
        self.start_button = discord.ui.Button(
            label="Start Jump",
            style=discord.ButtonStyle.success,
            custom_id=f"99k_host_start:{self.session_id}",
            row=0,
        )
        state = _automation_state(self.session_id)
        if bool(state.get("paused")):
            self.start_button.label = "Resume Jump"
        self.start_button.callback = self._on_start_or_resume
        self.add_item(self.start_button)

        manage_button = discord.ui.Button(
            label="Manage Jumpers",
            style=discord.ButtonStyle.primary,
            custom_id=f"99k_host_manage_jumpers:{self.session_id}",
            row=0,
        )
        manage_button.callback = self._on_manage_jumpers
        self.add_item(manage_button)

        self.pause_button = discord.ui.Button(
            label="Pause Jump",
            style=discord.ButtonStyle.secondary,
            custom_id=f"99k_host_pause:{self.session_id}",
            row=1,
        )
        self.pause_button.callback = self._on_pause
        self.add_item(self.pause_button)

        delete_button = discord.ui.Button(
            label="Delete This Jump",
            style=discord.ButtonStyle.danger,
            custom_id=f"99k_host_delete:{self.session_id}",
            row=1,
        )
        delete_button.callback = self._on_delete_confirm
        self.add_item(delete_button)

    async def _assert_host(self, interaction: discord.Interaction) -> dict | None:
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session or int(interaction.user.id) != int(session.get("host_discord_id") or 0):
            await _safe_edit_original(interaction, content="Only the jump host can use these controls.", view=None)
            return None
        return session

    async def _on_manage_jumpers(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        session = await self._assert_host(interaction)
        if not session:
            return
        await _safe_edit_original(interaction, content="Manage jumpers for this session:", view=Jump99kManageJumpersActionView(session_id=self.session_id))

    async def _on_start_or_resume(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        session = await self._assert_host(interaction)
        if not session:
            return
        state = _automation_state(self.session_id)
        if bool(state.get("running")) and not bool(state.get("paused")):
            await _safe_edit_original(interaction, content="Jump automation is already running.", view=None)
            return

        repo = JumpsRepository(get_pool())
        if not bool(state.get("running")):
            await repo.reset_jump_progress(self.session_id)
            ok, active_id = await _advance_to_next_jumper(repo=repo, session_id=self.session_id, actor_discord_id=int(interaction.user.id))
            if not ok:
                await _safe_edit_original(interaction, content="Could not start jump progression.", view=None)
                return
            state["active_discord_id"] = active_id
        state["running"] = True
        state["paused"] = False
        state["saw_nonzero_energy"] = False
        state["consecutive_low_energy_polls"] = 0
        state["last_transition_at"] = datetime.now(timezone.utc)

        if interaction.client:
            await _refresh_or_repost_roster_panel(interaction.client, self.session_id)
            await _refresh_99k_panel(interaction.client, self.session_id)
        await _safe_edit_original(interaction, content="✅ Jump automation running.", view=None)

    async def _on_pause(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        session = await self._assert_host(interaction)
        if not session:
            return
        state = _automation_state(self.session_id)
        if not bool(state.get("running")) or bool(state.get("paused")):
            await _safe_edit_original(interaction, content="Jump automation is not currently running.", view=None)
            return
        state["paused"] = True
        await _safe_edit_original(interaction, content="⏸️ Jump automation paused.", view=None)

    async def _on_delete_confirm(self, interaction: discord.Interaction) -> None:
        await _safe_defer_ephemeral(interaction)
        session = await self._assert_host(interaction)
        if not session:
            return
        await _safe_edit_original(interaction, content=f"Delete session #{self.session_id}?", view=Jump99kDeleteConfirmView(self.session_id))


class Jump99kDeleteConfirmView(discord.ui.View):
    def __init__(self, session_id: int):
        super().__init__(timeout=120)
        self.session_id = int(session_id)

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await _safe_defer_ephemeral(interaction)
        repo = JumpsRepository(get_pool())
        session = await repo.get_session(self.session_id)
        if not session or int(interaction.user.id) != int(session.get("host_discord_id") or 0):
            await _safe_edit_original(interaction, content="Only the jump host can use these controls.", view=None)
            return
        ok, _summary_lines = await _end_99k_session_via_shared_flow(
            interaction,
            session=session,
            actor_discord_id=int(interaction.user.id),
        )
        if not ok:
            await _safe_edit_original(interaction, content=f"Could not close session #{self.session_id}.", view=None)
            return
        await _safe_edit_original(interaction, content="✅ Jump deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await _safe_defer_ephemeral(interaction)
        await _safe_edit_original(interaction, content="Cancelled.", view=None)


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
            await _refresh_or_repost_roster_panel(interaction.client, self.session_id)
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
                    log.exception("Failed to disable closed session interaction message session_id=%s", self.session_id)
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
            log_event(
                log,
                logging.INFO,
                "jump99k.verify_payment.start",
                guild_id=interaction.guild_id,
                session_id=self.session_id,
                user_id=interaction.user.id,
                action="verify_payment",
                result="started",
            )

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
                await interaction.followup.send(
                    (
                        f"No qualifying payment found. Expected **{base_amount}x {item}** sent to **{host_torn_id}**. "
                        f"Searched transactions since <t:{since_ts}:R> (from session start window)."
                    ),
                    ephemeral=True,
                )
                log_event(
                    log,
                    logging.INFO,
                    "jump99k.verify_payment.no_match",
                    guild_id=interaction.guild_id,
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    action="verify_payment",
                    result="not_found",
                    search_window_since_ts=since_ts,
                    expected_item=item,
                    expected_amount=base_amount,
                    recipient_torn_id=host_torn_id,
                )
                return
            log_event(
                log,
                logging.INFO,
                "jump99k.verify_payment.match",
                guild_id=interaction.guild_id,
                session_id=self.session_id,
                user_id=interaction.user.id,
                action="verify_payment",
                result="matched",
                payment_item=item,
                paid_amount=paid_amount,
            )

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

            payer_torn = int((key_row or {}).get("torn_user_id") or 0) or None
            payer_torn_name = str((key_row or {}).get("torn_name") or "").strip() or None
            updated = await repo.mark_signup_payment_verified(
                session_id=self.session_id,
                discord_id=interaction.user.id,
                torn_user_id=payer_torn,
                torn_name=payer_torn_name,
            )
            log_event(
                log,
                logging.INFO,
                "jump99k.verify_payment.mark_paid",
                guild_id=interaction.guild_id,
                session_id=self.session_id,
                user_id=interaction.user.id,
                action="verify_payment",
                result="updated" if updated else "no_state_change",
            )
            if updated:
                interaction.client.dispatch(
                    "jump_99k_purchase_verified",
                    {
                        "guild_id": int(session.get("guild_id") or interaction.guild_id or 0),
                        "user_id": int(interaction.user.id),
                        "session_id": int(self.session_id),
                        "signup_id": int(signup.get("id") or 0) if signup.get("id") is not None else None,
                        "verified_at": datetime.now(timezone.utc),
                        "dedupe_key": f"jump_purchase:{int(self.session_id)}:{int(interaction.user.id)}",
                    },
                )
            if not updated:
                await interaction.followup.send("Your signup is not in a payable state. If already verified, you are good to go.", ephemeral=True)
                log_event(
                    log,
                    logging.INFO,
                    "jump99k.verify_payment.noop",
                    guild_id=interaction.guild_id,
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    action="verify_payment",
                    result="no_state_change",
                )
                return
            post_verify_warnings: list[str] = []
            receipts = PaymentReceiptService(db.pool)
            try:
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
                    receipt_hash=f"jump99k:{self.session_id}:{interaction.user.id}:{item}:{paid_amount}",
                )
                log_event(
                    log,
                    logging.INFO,
                    "jump99k.verify_payment.receipt",
                    guild_id=interaction.guild_id,
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    action="verify_payment",
                    result="ok",
                )
            except Exception:
                post_verify_warnings.append("receipt")
                log.exception(
                    "jump99k.verify_payment receipt write failed session_id=%s guild_id=%s user_id=%s",
                    self.session_id,
                    interaction.guild_id,
                    interaction.user.id,
                )

            if interaction.guild:
                granted = await _grant_private_channel_access(
                    interaction.guild, session, interaction.user.id
                )
                log_event(
                    log,
                    logging.INFO if granted else logging.WARNING,
                    "jump99k.verify_payment.private_access",
                    guild_id=interaction.guild_id,
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    action="verify_payment",
                    result="ok" if granted else "failed",
                )
                if not granted:
                    post_verify_warnings.append("private_channel")

            try:
                await _refresh_99k_panel(interaction.client, self.session_id)
                await _refresh_or_repost_roster_panel(interaction.client, self.session_id)
                log_event(
                    log,
                    logging.INFO,
                    "jump99k.verify_payment.refresh",
                    guild_id=interaction.guild_id,
                    session_id=self.session_id,
                    user_id=interaction.user.id,
                    action="verify_payment",
                    result="ok",
                )
            except Exception:
                post_verify_warnings.append("panel_refresh")
                log.exception(
                    "jump99k.verify_payment refresh failed session_id=%s guild_id=%s user_id=%s",
                    self.session_id,
                    interaction.guild_id,
                    interaction.user.id,
                )
            success_message = "✅ Payment verified for this 99k session."
            if post_verify_warnings:
                success_message += " Some follow-up updates were delayed."
            await interaction.followup.send(
                success_message,
                view=InsuranceOfferView(self.session_id, interaction.user.id),
                ephemeral=True,
            )
            log_event(
                log,
                logging.INFO,
                "jump99k.verify_payment.success",
                guild_id=interaction.guild_id,
                session_id=self.session_id,
                user_id=interaction.user.id,
                action="verify_payment",
                result="paid",
                paid_amount=paid_amount,
                payment_item=item,
            )
        except TornAPIError:
            await interaction.followup.send("Torn API may be down right now. Please try again in a minute.", ephemeral=True)
        except Exception:
            custom_id = str((interaction.data or {}).get("custom_id") or "")
            log.exception(
                "99k verify_payment failed guild_id=%s jump_id=%s user_id=%s custom_id=%s",
                interaction.guild_id,
                self.session_id,
                interaction.user.id if interaction.user else None,
                custom_id,
            )
            await interaction.followup.send(
                "Payment verification failed before confirmation. Please try again in a minute.",
                ephemeral=True,
            )


class Jump99kSignupView(discord.ui.View):
    def __init__(self, session_id: int, is_full: bool, is_closed: bool, is_locked: bool = False):
        super().__init__(timeout=None)
        self.session_id = session_id
        if is_closed:
            button = discord.ui.Button(label="Closed", style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"jump99k:join:{session_id}")
        elif is_locked:
            button = discord.ui.Button(label="Locked", style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"jump99k:join:{session_id}")
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
            if bool(session.get("signups_locked")):
                await _refresh_99k_panel(interaction.client, self.session_id)
                await interaction.followup.send("Signups are locked because the jump has started.", ephemeral=True)
                return

            signups = await repo.list_signups(self.session_id)
            signed_up = sum(1 for row in signups if row.get("status") in SIGNUP_ACTIVE_STATUSES)
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

            torn_user_id = int(key_row["torn_user_id"]) if key_row and key_row.get("torn_user_id") else None
            torn_name = str((key_row or {}).get("torn_name") or "").strip() or None
            await repo.create_or_restore_signup(
                session_id=self.session_id,
                guild_id=interaction.guild_id,
                discord_id=interaction.user.id,
                torn_user_id=torn_user_id,
                torn_name=torn_name,
                reserved_until=reserved_until,
            )
            log_event(
                log,
                logging.INFO,
                "jump99k.join.reserved",
                guild_id=interaction.guild_id,
                session_id=self.session_id,
                user_id=interaction.user.id,
                action="join",
                result="reserved",
                reserved_until=reserved_until.isoformat(),
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
        except SignupStatusSchemaMismatchError:
            log.exception(
                "99k join failed due to status schema mismatch session_id=%s guild_id=%s user_id=%s",
                self.session_id,
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await interaction.followup.send(
                "Join failed due to database schema mismatch. Ask an admin to run migrations.",
                ephemeral=True,
            )
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
        session_id: int | None = int(self.session["id"]) if self.session else None
        created_new_session = False
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            title = "✨ 99k Happy Jump ✨"
            try:
                slots = int(str(self.max_slots.value).strip())
            except ValueError:
                await interaction.followup.send(embed=create_error_embed("Invalid max slots", "Max slots must be a number from 1 to 7."), ephemeral=True)
                return
            if slots < 1 or slots > 7:
                await interaction.followup.send(embed=create_error_embed("Invalid max slots", "Max slots must be from 1 to 7."), ephemeral=True)
                return

            try:
                raw_payment_type = parse_payment_type(str(self.payment_type.value), allow_free=False)
            except ValueError as exc:
                await interaction.followup.send(embed=create_error_embed("Invalid payment type", str(exc)), ephemeral=True)
                return

            try:
                price_amount = int(str(self.spot_price.value).strip())
            except ValueError:
                await interaction.followup.send(embed=create_error_embed("Invalid spot price", "Spot price must be a whole number from 1 to 50."), ephemeral=True)
                return
            if price_amount < 1 or price_amount > 50:
                await interaction.followup.send(embed=create_error_embed("Invalid spot price", "Spot price must be between 1 and 50."), ephemeral=True)
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
                await interaction.followup.send(
                    "Invalid start time. Use MM-DD 8:00pm, include an offset like -0700, or paste a Discord timestamp like <t:...>.",
                    ephemeral=True,
                )
                return

            notes = str(self.notes.value).strip() or None
            announce_channel_id = _resolve_99k_signup_channel_id(self.settings, interaction.channel.id if interaction.channel else None)
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
                created_new_session = True

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
                    bot_member = await _resolve_bot_member(interaction.guild)
                    overwrites = {
                        interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        bot_member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
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
                    private_channel: discord.TextChannel | None = None
                    try:
                        private_channel = await interaction.guild.create_text_channel(
                            name=channel_name,
                            category=category,
                            overwrites=overwrites,
                            reason="99k jump session channel",
                        )
                        await repo.set_private_channel_id_only(int(session_id), channel_id=int(private_channel.id))

                        panel_embed, panel_view = await build_roster_panel(int(session_id), private_channel)
                        roster_msg = await private_channel.send(embed=panel_embed, view=panel_view)
                        await repo.set_roster_panel_message(
                            int(session_id),
                            channel_id=int(private_channel.id),
                            message_id=int(roster_msg.id),
                        )
                        await repo.touch_roster_refreshed(int(session_id))

                    except discord.Forbidden as channel_setup_error:
                        channel_id = int(private_channel.id) if private_channel else None
                        log.exception(
                            "99k private channel setup forbidden for session_id=%s channel_id=%s",
                            session_id,
                            channel_id,
                        )
                        if private_channel and interaction.guild:
                            try:
                                bot_member = await _resolve_bot_member(interaction.guild)
                                perms = private_channel.permissions_for(bot_member)
                                if perms.manage_channels:
                                    await private_channel.delete(reason="99k setup rollback after forbidden")
                            except Exception:
                                log.exception(
                                    "Failed to delete private channel after forbidden setup error session_id=%s channel_id=%s",
                                    session_id,
                                    channel_id,
                                )
                        raise RuntimeError(
                            "I couldn't finish setting up the private jump channel because I am missing permissions (Manage Channels / Send Messages / Embed Links). Please fix my permissions and retry."
                        ) from channel_setup_error
                    except Exception as channel_setup_error:
                        channel_id = int(private_channel.id) if private_channel else None
                        log.exception(
                            "99k private channel setup failed for session_id=%s channel_id=%s",
                            session_id,
                            channel_id,
                        )
                        if private_channel and interaction.guild:
                            try:
                                bot_member = await _resolve_bot_member(interaction.guild)
                                perms = private_channel.permissions_for(bot_member)
                                if perms.manage_channels:
                                    await private_channel.delete(reason=f"99k setup rollback failed: {type(channel_setup_error).__name__}")
                            except Exception:
                                log.exception(
                                    "Failed to delete private channel after setup error session_id=%s channel_id=%s",
                                    session_id,
                                    channel_id,
                                )
                        raise RuntimeError(
                            "I couldn't finish setting up the private jump channel. Please fix my channel permissions in this category (view/send/embed/manage) and retry."
                        ) from channel_setup_error

            target_channel_id = _resolve_99k_signup_channel_id(self.settings, interaction.channel.id if interaction.channel else None)
            await upsert_99k_announcement(
                bot=interaction.client,
                repo=repo,
                guild_id=int(interaction.guild_id),
                session_id=int(session_id),
                channel_id=target_channel_id,
                settings=self.settings,
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
            await interaction.followup.send(embed=create_success_embed("99k session saved", f"Session #{session_id} {verb}."), ephemeral=True)
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
            if _is_db_unavailable_error(e):
                log_event(
                    log,
                    logging.ERROR,
                    "jump99k.start.db_unavailable",
                    guild_id=interaction.guild_id,
                    user_id=interaction.user.id,
                    action="jump_start",
                    result="error",
                    error_type=type(e).__name__,
                    hint="check_db_connectivity_or_tls",
                    exc_info=True,
                )
                message = "Database is currently unavailable. The bot cannot start jumps until DB connectivity is restored."
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
                return

            if created_new_session and session_id is not None:
                try:
                    await repo.update_session_status(int(session_id), "needs_cleanup")
                    log_event(
                        log,
                        logging.WARNING,
                        "jump99k.start.needs_cleanup",
                        guild_id=interaction.guild_id,
                        user_id=interaction.user.id,
                        session_id=int(session_id),
                        action="jump_start",
                        result="partial",
                        hint="db_session_created_discord_setup_failed",
                    )
                except Exception:
                    log.exception("Failed marking session as needs_cleanup session_id=%s", session_id)

            log.exception("99k session modal submit failed: %s", e)
            if isinstance(e, RuntimeError):
                err = create_error_embed("99k start failed", str(e))
            else:
                err = create_error_embed("99k start failed", f"{type(e).__name__}: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(embed=err, ephemeral=True)
            else:
                await interaction.response.send_message(embed=err, ephemeral=True)




jump99k_group = app_commands.Group(name="99k", description="99k happy jump commands")


@jump99k_group.command(name="start", description="Create a 99k jump session")
@require_command_access(required_role_setting_keys=("host99k_role_id", "host_role_id"), failure_message="Administrator or the configured 99k Host role is required.")
async def jump99k_start(interaction: discord.Interaction):
    settings = await GuildSettingsRepository(get_database()).get_or_create(interaction.guild_id)
    if not await assert99kHost(interaction, {"host_role_id": settings.get("host99k_role_id")}):
        return

    report = validate_99k_permissions(
        interaction.guild,
        bot.user,
        signup_channel_id=_resolve_99k_signup_channel_id(settings, interaction.channel.id if interaction.channel else None),
        announce_channel_id=int(settings.get("jump_announce_channel_id") or 0) or None,
        private_category_id=int(settings.get("jump_99k_private_category_id") or 0) or None,
    )
    critical_missing = []
    for name, row in report.get("channels", {}).items():
        missing = [m for m in row.get("missing_permissions", []) if m not in {"channel_not_configured", "missing_channel"}]
        if missing:
            critical_missing.append(f"{name}: {', '.join(missing)}")
    if critical_missing:
        await interaction.response.send_message(
            embed=create_error_embed("99k start blocked", "Missing required permissions:\n" + "\n".join(critical_missing)),
            ephemeral=True,
        )
        return

    log_event(log, logging.INFO, "jump99k.start", guild_id=interaction.guild_id, user_id=interaction.user.id, action="start", result="ok")

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
@require_command_access(required_role_setting_keys=("host99k_role_id", "host_role_id"), failure_message="Administrator or the configured 99k Host role is required.")
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


@jump99k_group.command(name="list", description="List open 99k sessions")
async def jump99k_list(interaction: discord.Interaction):
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
        price_item = _format_99k_price_item_label(session.get("price_item"))
        price_amount = int(session.get("price_amount") or 0)
        status = str(session.get("status") or "open").title()
        join_help = "Use the active 99k panel Join button to reserve a spot."
        summary_lines.append(
            f"`#{session['id']}` **{session.get('title') or 'Untitled'}** • Start: {scheduled} • "
            f"Status: {status} • Spots: {signup_count}/{session.get('max_slots') or 0} • "
            f"Price: {price_amount} {price_item} • {join_help}"
        )

    description = "\n".join(summary_lines)
    if len(sessions) > 10:
        description += f"\n…and {len(sessions) - 10} more open session(s)."

    embed = create_info_embed("99k open sessions", description)

    await interaction.followup.send(embed=embed, ephemeral=True)


@jump99k_group.command(name="end", description="End a specific 99k session by ID")
@require_command_access(required_role_setting_keys=("host99k_role_id", "host_role_id"), failure_message="Administrator or the configured 99k Host role is required.")
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

    ok, summary_lines = await _end_99k_session_via_shared_flow(
        interaction,
        session=session,
        actor_discord_id=int(interaction.user.id),
    )
    if not ok:
        await interaction.followup.send(
            embed=create_error_embed("Could not close", summary_lines[0]),
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embed=create_success_embed("99k session ended", "\n".join(summary_lines)),
        ephemeral=True,
    )


@bot.tree.command(name="99k_reset_progress", description="Reset Start/End progress for a 99k session")
@require_command_access(required_role_setting_keys=("host99k_role_id", "host_role_id"), failure_message="Administrator or the configured 99k Host role is required.")
@app_commands.describe(session_id="Optional 99k session ID (defaults to session mapped to this channel)")
async def jump99k_reset_progress(interaction: discord.Interaction, session_id: int | None = None):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    repo = JumpsRepository(get_pool())

    target_session_id: int | None = int(session_id) if session_id is not None else None
    if target_session_id is not None and target_session_id <= 0:
        await interaction.followup.send("Session ID must be a positive number.", ephemeral=True)
        return

    if target_session_id is None:
        if not interaction.channel_id:
            await interaction.followup.send("Could not resolve the current channel.", ephemeral=True)
            return
        target_session_id = await repo.get_session_id_by_channel(int(interaction.channel_id))
        if not target_session_id:
            await interaction.followup.send("No 99k session is mapped to this channel. Provide /99k_reset_progress session_id:<id>.", ephemeral=True)
            return

    session = await repo.get_session(int(target_session_id))
    if not session or int(session.get("guild_id") or 0) != int(interaction.guild_id):
        await interaction.followup.send("Session not found for this server.", ephemeral=True)
        return

    if not await can_manage_99k_session(interaction, session):
        await interaction.followup.send("Not allowed.", ephemeral=True)
        return

    await repo.reset_jump_progress(int(target_session_id))

    refreshed = bool(interaction.client) and await _refresh_or_repost_roster_panel(interaction.client, int(target_session_id))
    if refreshed:
        await interaction.followup.send("✅ Progress reset. Start 1 is available again.", ephemeral=True)
        return

    await interaction.followup.send(
        "Progress reset, but I could not refresh the roster panel automatically.",
        ephemeral=True,
    )


bot.tree.add_command(jump99k_group)

@bot.tree.command(name="policy_create", description="Create an insurance policy (Admin only)")
@app_commands.default_permissions(administrator=True)
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
@app_commands.describe(category="Filter by insurer category")
@app_commands.choices(
    category=[
        app_commands.Choice(name="All insurers", value="all"),
        app_commands.Choice(name="99k jump", value="99k jump"),
        app_commands.Choice(name="Happy jump", value="Happy jump"),
        app_commands.Choice(name="Xanax stack", value="Xanax stack"),
        app_commands.Choice(name="Ecstasy only", value="Ecstasy only"),
        app_commands.Choice(name="Multi day", value="Multi day"),
        app_commands.Choice(name="2 hours after purchase", value="2 hours after purchase"),
    ]
)
async def insurers(
    interaction: discord.Interaction,
    category: app_commands.Choice[str] = None,
):
    if not interaction.guild_id:
        await interaction.response.send_message(embed=create_error_embed("Unavailable", "This command only works in a server."), ephemeral=True)
        return

    selected_category = None if not category or category.value == "all" else category.value
    view = InsurerBrowserView(
        guild_id=interaction.guild_id,
        category=selected_category,
        timeout=300,
    )
    embed = await view.build_embed(bot)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="application_review", description="Review insurer/host99k applications (Admin only)")
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
@require_command_access(include_configured_admin_roles=True, allow_manage_guild=True)
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
    if not await _worker_db_ready("cleanup_worker"):
        return
    """Background cleanup task for 99k private channels and stale buttons."""
    worker_slot = db_heavy_worker_slot("cleanup_worker")
    await worker_slot.__aenter__()
    try:
        repo = JumpsRepository(get_pool())
        sessions = await repo.list_non_open_sessions_for_cleanup()
        for session in sessions:
            await _disable_99k_session_messages(bot, session, status_text=f"Session {session.get('status')}")
            session_id = int(session["id"])
            guild = bot.get_guild(int(session["guild_id"]))
            private_channel_id = int(session["private_channel_id"]) if session.get("private_channel_id") else None
            roster_channel_id = int(session["roster_channel_id"]) if session.get("roster_channel_id") else None
            roster_message_id = int(session["roster_message_id"]) if session.get("roster_message_id") else None

            panel_deleted = True
            if roster_message_id:
                panel_channel_id = roster_channel_id or private_channel_id
                if not guild or not panel_channel_id:
                    panel_deleted = False
                else:
                    try:
                        panel_channel = guild.get_channel(panel_channel_id) or await guild.fetch_channel(panel_channel_id)
                        bot_member = await _resolve_bot_member(guild)
                        perms = panel_channel.permissions_for(bot_member)
                        if not perms.view_channel or not perms.read_message_history:
                            panel_deleted = False
                            log.warning("cleanup_worker missing_read_history session=%s", session_id)
                        else:
                            panel_message = await panel_channel.fetch_message(roster_message_id)
                            await panel_message.delete(reason="99k session finished")
                    except discord.NotFound:
                        panel_deleted = True
                    except discord.Forbidden:
                        panel_deleted = False
                        log.warning("cleanup_worker forbidden deleting panel session=%s", session_id)
                    except Exception:
                        panel_deleted = False
                        log.exception("Failed cleanup delete for 99k panel session=%s", session_id)

            channel_deleted = True
            if private_channel_id:
                if not guild:
                    channel_deleted = False
                else:
                    try:
                        private_channel = guild.get_channel(private_channel_id) or await guild.fetch_channel(private_channel_id)
                        bot_member = await _resolve_bot_member(guild)
                        perms = private_channel.permissions_for(bot_member)
                        if not perms.manage_channels:
                            channel_deleted = False
                            log.warning("cleanup_worker missing_manage_channels session=%s", session_id)
                        else:
                            await private_channel.delete(reason="99k session finished")
                    except discord.NotFound:
                        channel_deleted = True
                    except discord.Forbidden:
                        channel_deleted = False
                        log.warning("cleanup_worker forbidden deleting private channel session=%s", session_id)
                    except Exception:
                        channel_deleted = False
                        log.exception("Failed cleanup delete for 99k private channel session=%s", session_id)

            if panel_deleted:
                await repo.clear_roster_message(session_id)
            if channel_deleted:
                await repo.clear_private_channel_only(session_id)
            if panel_deleted and channel_deleted:
                await repo.mark_cleaned(session_id)
    except Exception:
        log.exception("cleanup_worker failed")
    finally:
        await worker_slot.__aexit__(None, None, None)


@cleanup_worker.before_loop
async def before_cleanup_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("cleanup_worker")


@tasks.loop(seconds=45)
async def cleanup_retry_worker():
    if not await _worker_db_ready("cleanup_retry_worker"):
        return
    worker_slot = db_heavy_worker_slot("cleanup_retry_worker")
    await worker_slot.__aenter__()
    try:
        repo = JumpsRepository(get_pool())
        tasks_due = await repo.list_due_cleanup_tasks(limit=50)
        for task in tasks_due:
            guild = bot.get_guild(int(task["guild_id"]))
            if guild is None:
                continue
            if str(task.get("task_type")) == "delete_message":
                ok, status = await delete_message_safe(guild, task.get("channel_id"), task.get("message_id"), "99k cleanup retry", {"session_id": task.get("session_id")})
            else:
                ok, status = await delete_channel_safe(guild, task.get("channel_id"), "99k cleanup retry", {"session_id": task.get("session_id")})
            task_id = int(task["id"])
            session_id = int(task.get("session_id") or 0)
            if ok:
                await repo.delete_cleanup_task(task_id)
                if str(task.get("task_type")) == "delete_message":
                    await repo.clear_cleanup_message_target(session_id=session_id, channel_id=task.get("channel_id"), message_id=task.get("message_id"))
                else:
                    await repo.clear_cleanup_channel_target(session_id=session_id, channel_id=task.get("channel_id"))
                log_event(log, logging.INFO, "jump99k.cleanup_retry.task", guild_id=task.get("guild_id"), session_id=session_id, action=str(task.get("task_type")), result="success")
                continue
            attempts = int(task.get("attempts") or 0) + 1
            if "missing_perms" in str(status):
                attempts = max(attempts, 8)
                status = f"forbidden:{status}; grant bot permissions and retry /99k end"
            if status in {"missing_channel", "already_deleted", "missing_ids"}:
                await repo.delete_cleanup_task(task_id)
                log_event(log, logging.INFO, "jump99k.cleanup_retry.task", guild_id=task.get("guild_id"), session_id=session_id, action=str(task.get("task_type")), result="not_found_treated_success")
                continue
            if attempts >= 10:
                log.error("cleanup_retry_worker max attempts reached task_id=%s session_id=%s status=%s", task.get("id"), task.get("session_id"), status)
            await repo.update_cleanup_task_failure(task_id=task_id, attempts=attempts, error=status)
            log_event(log, logging.INFO, "jump99k.cleanup_retry.task", guild_id=task.get("guild_id"), session_id=session_id, action=str(task.get("task_type")), result="retry_scheduled", attempts=attempts)
    except RuntimeError as exc:
        if "not initialized" in str(exc):
            log.warning("cleanup_retry_worker waiting for database init")
            await asyncio.sleep(5)
    except Exception:
        log.exception("cleanup_retry_worker failed")
    finally:
        await worker_slot.__aexit__(None, None, None)


@cleanup_retry_worker.before_loop
async def before_cleanup_retry_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("cleanup_retry_worker")


@tasks.loop(seconds=3)
async def roster_panel_refresh_worker():
    if not await _worker_db_ready("roster_panel_refresh_worker"):
        return
    """Refresh active 99k roster panels and button states."""
    worker_slot = db_heavy_worker_slot("roster_panel_refresh_worker")
    await worker_slot.__aenter__()
    try:
        repo = JumpsRepository(get_pool())
        sessions = await repo.list_active_sessions_with_roster_panel()
        for session in sessions:
            session_id = int(session["id"])
            jump_started = await _session_jump_started(repo, session_id)
            if jump_started:
                continue
            try:
                await _refresh_or_repost_roster_panel(bot, session_id)
            except Exception:
                log.exception("Roster auto-refresh failed session=%s", session_id)
            finally:
                await asyncio.sleep(0.2)
    except Exception:
        log.exception("roster_panel_refresh_worker failed")
    finally:
        await worker_slot.__aexit__(None, None, None)


@roster_panel_refresh_worker.before_loop
async def before_roster_panel_refresh_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("roster_panel_refresh_worker")


@tasks.loop(seconds=3)
async def readiness_worker():
    if not await _worker_db_ready("readiness_worker"):
        return
    """Refresh readiness snapshots for active 99k sessions using dynamic session cadence."""
    worker_slot = db_heavy_worker_slot("readiness_worker")
    await worker_slot.__aenter__()
    try:
        db = get_database()
        repo = JumpsRepository(db.pool)
        users_repo = UsersRepository(db.pool)
        torn_api = get_torn_api()
        security = get_security_manager()
        now = datetime.now(timezone.utc)

        active_sessions: dict[int, dict] = {}
        for guild in bot.guilds:
            session = await repo.get_active_session(guild.id)
            if not session:
                continue
            active_sessions[int(session["id"])] = session

        active_ids = set(active_sessions.keys())
        for stale_id in list(_READINESS_SESSION_NEXT_DUE.keys()):
            if stale_id not in active_ids:
                _READINESS_SESSION_NEXT_DUE.pop(stale_id, None)

        for session_id, session in active_sessions.items():
            next_due = _READINESS_SESSION_NEXT_DUE.get(session_id)
            if next_due and next_due > now:
                continue

            jump_started = await _session_jump_started(repo, session_id)

            signups = await repo.list_signups(session_id)
            participant_ids = {int(session.get("host_discord_id") or 0)}
            active_signup_ids: list[int] = []
            for s in signups:
                if s.get("status") in SIGNUP_ACTIVE_STATUSES:
                    discord_id = int(s.get("discord_id") or 0)
                    participant_ids.add(discord_id)
                    active_signup_ids.append(discord_id)

            readiness_before = {int(r.get("discord_id") or 0): str(r.get("status_text") or "") for r in await repo.list_readiness(session_id)}

            for discord_id in sorted(d for d in participant_ids if d > 0):
                try:
                    key_row = await users_repo.get_user_api_key(discord_id)
                    if not key_row:
                        throttle_key = (session_id, discord_id)
                        expiry = _READINESS_MISSING_KEY_LOG_CACHE.get(throttle_key)
                        if not expiry or expiry <= now:
                            log.info(
                                "Skipping readiness refresh due to missing API key discord_id=%s guild_id=%s session_id=%s",
                                discord_id,
                                session.get("guild_id"),
                                session_id,
                            )
                            _READINESS_MISSING_KEY_LOG_CACHE[throttle_key] = now + timedelta(hours=1)
                        continue

                    key_data = dict(key_row)
                    if "encrypted_key" not in key_data and "api_key_encrypted" in key_data:
                        key_data["encrypted_key"] = key_data["api_key_encrypted"]

                    api_key = security.decrypt_api_key(key_data["encrypted_key"])
                    try:
                        user_data = await torn_api.get_user_data(
                            api_key,
                            audit_discord_id=int(discord_id),
                            audit_torn_id=int(key_data.get("torn_user_id") or 0) or None,
                            audit_context="jump_readiness",
                            audit_query_meta={},
                        )
                        energy = int((user_data or {}).get("bars", {}).get("energy", {}).get("current", 0) or 0)
                        energy_max = int((user_data or {}).get("bars", {}).get("energy", {}).get("maximum", 0) or 0)
                        drug_cd = int((user_data or {}).get("cooldowns", {}).get("drug", 0) or 0)
                        booster_cd = int((user_data or {}).get("cooldowns", {}).get("booster", 0) or 0)
                        status_text = _get_readiness_status({"energy": energy, "energy_max": energy_max}, drug_cd)
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
                    except TornAPIPermissionError as exc:
                        await repo.upsert_readiness_snapshot(
                            session_id=session_id,
                            guild_id=int(session["guild_id"]),
                            discord_id=discord_id,
                            energy=0,
                            energy_max=0,
                            drug_cooldown=0,
                            booster_cooldown=0,
                            status_text="API key missing Bars/Cooldowns permissions",
                        )
                        throttle_key = (session_id, discord_id)
                        expiry = _READINESS_PERMISSION_LOG_CACHE.get(throttle_key)
                        if not expiry or expiry <= now:
                            log.info("Readiness refresh permission error guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s", session.get("guild_id"), session_id, discord_id, type(exc).__name__, exc)
                            _READINESS_PERMISSION_LOG_CACHE[throttle_key] = now + timedelta(hours=1)
                    except TornAPIRateLimitError as exc:
                        log.debug("Readiness refresh skipped due to rate limit guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s", session.get("guild_id"), session_id, discord_id, type(exc).__name__, exc)
                    except TornAPIError as exc:
                        throttle_key = (session_id, discord_id)
                        expiry = _READINESS_PERMISSION_LOG_CACHE.get(throttle_key)
                        if not expiry or expiry <= now:
                            log.info("Readiness refresh Torn API error guild_id=%s session_id=%s discord_id=%s error_type=%s error=%s", session.get("guild_id"), session_id, discord_id, type(exc).__name__, exc)
                            _READINESS_PERMISSION_LOG_CACHE[throttle_key] = now + timedelta(hours=1)
                    except Exception:
                        log.exception("Unexpected readiness refresh failure guild_id=%s session_id=%s discord_id=%s", session.get("guild_id"), session_id, discord_id)
                except Exception:
                    log.exception("Failed to prepare readiness refresh guild_id=%s session_id=%s discord_id=%s", session.get("guild_id"), session_id, discord_id)
                await asyncio.sleep(0.35)

            readiness_after_rows = await repo.list_readiness(session_id)
            readiness_after = {int(r.get("discord_id") or 0): str(r.get("status_text") or "") for r in readiness_after_rows}
            next_seconds = 3 if jump_started else 15
            _READINESS_SESSION_NEXT_DUE[session_id] = datetime.now(timezone.utc) + timedelta(seconds=next_seconds)

            if readiness_before != readiness_after and bot:
                await _refresh_or_repost_roster_panel(bot, session_id)
    except Exception as e:
        log.error(f"Readiness worker error: {e}", exc_info=True)
    finally:
        await worker_slot.__aexit__(None, None, None)


@readiness_worker.before_loop
async def before_readiness_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("readiness_worker")




@tasks.loop(seconds=3)
async def jump_automation_worker():
    if not await _worker_db_ready("jump_automation_worker"):
        return
    repo = JumpsRepository(get_pool())
    users_repo = UsersRepository(get_pool())
    for session_id, state in list(_JUMP_AUTOMATION_STATE.items()):
        if not bool(state.get("running")) or bool(state.get("paused")):
            continue
        session = await repo.get_session(int(session_id))
        active_discord_id = int(state.get("active_discord_id") or 0)
        if not session or active_discord_id <= 0:
            continue
        snapshot = await _fetch_and_upsert_user_readiness_snapshot(
            repo=repo,
            users_repo=users_repo,
            session_id=int(session_id),
            guild_id=int(session.get("guild_id") or 0),
            discord_id=active_discord_id,
        )
        if snapshot is None:
            continue
        energy = int(snapshot.get("energy") or 0)
        saw_nonzero, low_count, should_finish = _apply_energy_poll(
            saw_nonzero_energy=bool(state.get("saw_nonzero_energy")),
            consecutive_low_energy_polls=int(state.get("consecutive_low_energy_polls") or 0),
            energy=energy,
        )
        state["saw_nonzero_energy"] = saw_nonzero
        state["consecutive_low_energy_polls"] = low_count

        if should_finish:
            progress = await repo.get_jump_progress(int(session_id))
            host_id = int(session.get("host_discord_id") or 0)
            if active_discord_id == host_id:
                end_pos = 1
            else:
                end_pos = None
                for idx, row in enumerate(progress.get("signups") or [], start=2):
                    if int(row.get("discord_id") or 0) == active_discord_id:
                        end_pos = idx
                        break
            if end_pos is None:
                continue
            await repo.run_jump_transition_by_position(session_id=int(session_id), position=end_pos, action="end", actor_discord_id=host_id)
            ok, next_id = await _advance_to_next_jumper(repo=repo, session_id=int(session_id), actor_discord_id=host_id)
            state["saw_nonzero_energy"] = False
            state["consecutive_low_energy_polls"] = 0
            if ok and next_id:
                state["active_discord_id"] = int(next_id)
                guild = bot.get_guild(int(session.get("guild_id") or 0))
                if guild:
                    channel_id = int(session.get("private_channel_id") or session.get("roster_channel_id") or 0)
                    channel = guild.get_channel(channel_id)
                    if channel:
                        roster_rows = await repo.list_roster_signups_with_readiness(int(session_id))
                        content = await _build_jump_transition_notification(
                            users_repo=users_repo,
                            session=session,
                            roster_rows=roster_rows,
                            previous_discord_id=active_discord_id,
                            next_discord_id=int(next_id),
                            guild=guild,
                        )
                        await safe_send_channel(guild, int(channel_id), content=content)
            else:
                state["running"] = False
                state["paused"] = False
                guild = bot.get_guild(int(session.get("guild_id") or 0))
                if guild:
                    channel_id = int(session.get("private_channel_id") or session.get("roster_channel_id") or 0)
                    channel = guild.get_channel(channel_id)
                    if channel:
                        await safe_send_channel(guild, int(channel_id), content="✅ Jump session complete.")
        await _refresh_or_repost_roster_panel(bot, int(session_id))


@jump_automation_worker.before_loop
async def before_jump_automation_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("jump_automation_worker")

@tasks.loop(seconds=30)
async def auto_verify_99k_payments():
    if not await _worker_db_ready("auto_verify_99k_payments"):
        return

    db = get_database()

    async def _run_once() -> dict[str, int]:
        repo = JumpsRepository(get_pool())
        users_repo = UsersRepository(db.pool)
        security = get_security_manager()
        torn_api = get_torn_api()

        verified = 0
        finalized_priority = 0

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
                    log.warning(
                        "Skipping auto_verify_99k_payments due to missing API key discord_id=%s guild_id=%s",
                        participant_id,
                        signup.get("guild_id"),
                    )
                    continue

                host_key = await users_repo.get_user_api_key(int(signup["host_discord_id"]))
                host_torn_id = int(host_key["torn_user_id"]) if host_key and host_key.get("torn_user_id") else 0
                if not host_torn_id:
                    continue

                api_key = security.decrypt_api_key(encrypted_key)
                signup_created_at = signup.get("signup_created_at") or signup.get("created_at")
                since_ts = int((signup_created_at - timedelta(seconds=60)).timestamp())

                base_amount = int(signup.get("price_amount") or 0)
                priority_increment = int(signup.get("priority_increment") or 1)
                priority_amount = base_amount + priority_increment

                item = str(signup.get("price_item", "")).lower()
                payment = None
                paid_amount = base_amount

                if item == "xanax":
                    payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                    if payment:
                        paid_amount = priority_amount
                    else:
                        payment = await torn_api.verify_xanax_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
                        paid_amount = base_amount if payment else base_amount
                elif item == "erotic_dvd":
                    payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, priority_amount, since_timestamp=since_ts)
                    if payment:
                        paid_amount = priority_amount
                    else:
                        payment = await torn_api.verify_dvd_payment(api_key, host_torn_id, base_amount, since_timestamp=since_ts)
                        paid_amount = base_amount if payment else base_amount
                else:
                    continue

                if not payment:
                    log_event(
                        log,
                        logging.DEBUG,
                        "jump99k.auto_verify.no_match",
                        guild_id=signup.get("guild_id"),
                        session_id=session_id,
                        user_id=participant_id,
                        action="auto_verify",
                        result="not_found",
                    )
                    continue

                if paid_amount == priority_amount:
                    await repo.finalize_priority(
                        session_id=session_id,
                        buyer_discord_id=participant_id,
                        signup_id=int(signup["id"]),
                    )
                    finalized_priority += 1

                updated = await repo.mark_signup_payment_verified(
                    session_id=session_id,
                    discord_id=participant_id,
                    torn_user_id=int((key_row or {}).get("torn_user_id") or 0) or None,
                    torn_name=str((key_row or {}).get("torn_name") or "").strip() or None,
                )
                if updated:
                    bot.dispatch(
                        "jump_99k_purchase_verified",
                        {
                            "guild_id": int(signup.get("guild_id") or 0),
                            "user_id": int(participant_id),
                            "session_id": int(session_id),
                            "signup_id": int(signup.get("id") or 0) if signup.get("id") is not None else None,
                            "verified_at": datetime.now(timezone.utc),
                            "dedupe_key": f"jump_purchase:{int(session_id)}:{int(participant_id)}",
                        },
                    )
                if not updated:
                    log_event(
                        log,
                        logging.DEBUG,
                        "jump99k.auto_verify.noop",
                        guild_id=signup.get("guild_id"),
                        session_id=session_id,
                        user_id=participant_id,
                        action="auto_verify",
                        result="no_state_change",
                    )
                    continue

                verified += 1

                log_event(
                    log,
                    logging.INFO,
                    "jump99k.auto_verify.match",
                    guild_id=signup.get("guild_id"),
                    session_id=session_id,
                    user_id=participant_id,
                    action="auto_verify",
                    result="matched",
                    payment_item=item,
                    paid_amount=paid_amount,
                )

                payer_torn = int(key_row.get("torn_user_id") or 0) or None
                try:
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
                        receipt_hash=f"jump99k:{session_id}:{participant_id}:{item}:{paid_amount}",
                    )
                    log_event(
                        log,
                        logging.INFO,
                        "jump99k.auto_verify.receipt",
                        guild_id=signup.get("guild_id"),
                        session_id=session_id,
                        user_id=participant_id,
                        action="auto_verify",
                        result="ok",
                    )
                except Exception:
                    log.exception(
                        "jump99k.auto_verify receipt write failed session_id=%s participant_id=%s",
                        session_id,
                        participant_id,
                    )

                guild = bot.get_guild(int(signup["guild_id"]))
                session = await repo.get_session(session_id)
                if guild and session:
                    granted = await _grant_private_channel_access(guild, session, participant_id)
                    log_event(
                        log,
                        logging.INFO if granted else logging.WARNING,
                        "jump99k.auto_verify.private_access",
                        guild_id=signup.get("guild_id"),
                        session_id=session_id,
                        user_id=participant_id,
                        action="auto_verify",
                        result="ok" if granted else "failed",
                    )
                    try:
                        await _refresh_99k_panel(bot, session_id)
                        await _refresh_or_repost_roster_panel(bot, session_id)
                    except Exception:
                        log.exception(
                            "jump99k.auto_verify refresh failed session_id=%s participant_id=%s",
                            session_id,
                            participant_id,
                        )
            except Exception as entry_err:
                log.warning(
                    "Auto verify failed for signup %s/%s: %s",
                    signup.get("session_id"),
                    signup.get("participant_discord_id"),
                    entry_err,
                )
                continue

        return {
            "scanned": len(pending),
            "verified": verified,
            "finalized_priority": finalized_priority,
        }

    worker_slot = db_heavy_worker_slot("auto_verify_99k_payments")
    await worker_slot.__aenter__()
    try:
        acquired, result = await run_with_advisory_lock(db, "worker:jump99k:auto_verify", _run_once)
        if not acquired:
            return

        scanned = int((result or {}).get("scanned", 0))
        verified = int((result or {}).get("verified", 0))
        finalized_priority = int((result or {}).get("finalized_priority", 0))

        if verified > 0 or finalized_priority > 0:
            log_event(
                log,
                logging.INFO,
                "jump99k.auto_verify.summary",
                action="auto_verify",
                result="ok",
                scanned=scanned,
                verified=verified,
                finalized_priority=finalized_priority,
            )
        else:
            log.debug("jump99k.auto_verify tick scanned=%s verified=0", scanned)
    except Exception as e:
        log_event(
            log,
            logging.ERROR,
            "jump99k.auto_verify.failed",
            action="auto_verify",
            result="error",
            error_type=type(e).__name__,
            exc_info=True,
        )
    finally:
        await worker_slot.__aexit__(None, None, None)


@auto_verify_99k_payments.before_loop
async def before_auto_verify_99k_payments():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("auto_verify_99k_payments")


@tasks.loop(seconds=60)
async def overdose_monitor():
    if not await _worker_db_ready("overdose_monitor"):
        return
    """Track overdose events for open 99k sessions using shared overdose tracker."""
    worker_slot = db_heavy_worker_slot("overdose_monitor")
    await worker_slot.__aenter__()
    try:
        db = get_database()
        jumps_repo = JumpsRepository(db.pool)
        users_repo = UsersRepository(db.pool)
        tracker = OverdoseTracker(
            users_repo=users_repo,
            overdose_repo=OverdoseRepository(db.pool),
            jumps_repo=jumps_repo,
        )

        sessions = await jumps_repo.list_open_sessions_for_monitoring()
        now = datetime.now(timezone.utc)
        for session in sessions:
            session_id = int(session["id"])
            guild_id = int(session["guild_id"])
            guild = bot.get_guild(guild_id)
            signups = await jumps_repo.list_signups(session_id)
            for signup in signups:
                if not bool(signup.get("payment_verified")):
                    continue
                if signup.get("status") not in SIGNUP_ACTIVE_STATUSES:
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
    finally:
        await worker_slot.__aexit__(None, None, None)


@overdose_monitor.before_loop
async def before_overdose_monitor():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("overdose_monitor")


@tasks.loop(seconds=config.INSURANCE_CHECK_INTERVAL)
async def insurance_monitor():
    """Legacy monitor disabled: OD notifications are handled by overdose_monitor."""
    return


@insurance_monitor.before_loop
async def before_insurance_monitor():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)


@tasks.loop(seconds=config.RAFFLE_COMPLETION_INTERVAL)
async def raffle_completion_worker():
    if not await _worker_db_ready("raffle_completion_worker"):
        return
    """Check for completed raffles and draw winners."""
    worker_slot = db_heavy_worker_slot("raffle_completion_worker")
    await worker_slot.__aenter__()
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
    finally:
        await worker_slot.__aexit__(None, None, None)


@raffle_completion_worker.before_loop
async def before_raffle_completion_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("raffle_completion_worker")


# ============================================================================
# WORKER HELPERS
# ============================================================================



def _all_active_non_finished_ready(*, active_non_finished_discord_ids: list[int], readiness_rows: list[dict]) -> bool:
    if not active_non_finished_discord_ids:
        return False
    readiness_by_discord = {
        int(row.get("discord_id") or 0): str(row.get("status_text") or "").strip().lower()
        for row in readiness_rows
    }
    return all(readiness_by_discord.get(discord_id, "").startswith("ready") for discord_id in active_non_finished_discord_ids)


def _readiness_poll_seconds(*, all_active_non_finished_ready: bool, active_seconds: int, hot_seconds: int) -> int:
    if all_active_non_finished_ready:
        return max(5, int(hot_seconds))
    return max(10, int(active_seconds))

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
    
    try:
        draw_result = await raffles_repo.draw_raffle_winner_atomic(raffle_id)
        draw_state = draw_result.get("state")
        winner = draw_result.get("winner")

        if draw_state in {"already_drawn", "not_drawable", "not_ready", "not_found"}:
            log.info("Skipping raffle draw raffle_id=%s state=%s", raffle_id, draw_state)
            return
        
        # Log the draw
        await audit_repo.log_audit(
            None,  # System action (no actor)
            "raffle_auto_drawn",
            "raffle",
            raffle_id,
            {"winner_discord_id": winner['discord_id'] if winner else None, "draw_state": draw_state},
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

        try:
            me = await _resolve_bot_member(guild)
        except RuntimeError:
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
        raise


async def main():
    """Main entry point for the Discord bot process."""
    config.validate_config()
    log.info("Starting Discord bot service")

    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())


@tasks.loop(minutes=30)
async def departed_member_reconciliation_worker():
    if not await _worker_db_ready("departed_member_reconciliation_worker"):
        return
    cleanup = MemberCleanupService(get_pool())
    for guild in bot.guilds:
        try:
            members = guild.members
            if not members:
                members = [m async for m in guild.fetch_members(limit=None)]
            present_ids = {int(m.id) for m in members if not m.bot}
            known_ids = await cleanup.list_known_guild_user_ids(int(guild.id))
            stale = [uid for uid in known_ids if uid not in present_ids]
            removed = 0
            for uid in stale[:500]:
                summary = await cleanup.cleanup_departed_member(int(guild.id), int(uid))
                removed += sum(int(v or 0) for v in summary.values())
                await asyncio.sleep(0.05)
            if stale:
                log.info(
                    "Departed-member reconciliation guild_id=%s stale_users=%s removed_rows=%s",
                    guild.id,
                    len(stale),
                    removed,
                )
        except Exception:
            log.exception("Departed-member reconciliation failed guild_id=%s", guild.id)


@departed_member_reconciliation_worker.before_loop
async def before_departed_member_reconciliation_worker():
    await bot.wait_until_ready()
    await wait_until_initialized(timeout=30.0)
    await sleep_startup_jitter("departed_member_reconciliation_worker")
