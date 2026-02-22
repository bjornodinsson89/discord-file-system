"""Happy Jumper bot configuration (Discord-only service)."""

import os
import ssl
from urllib.parse import parse_qs, urlparse
import logging

try:
    import certifi
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal test envs
    certifi = None


log = logging.getLogger("happy_jumper.config")


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def safe_int_env(name: str, default: int | None = None, *, allow_blank: bool = True) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    if stripped == "":
        return default if allow_blank else None
    try:
        return int(stripped)
    except (TypeError, ValueError):
        return default


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = safe_int_env("GUILD_ID", default=None, allow_blank=True)
CLEAN_COMMANDS = _env_flag("CLEAN_COMMANDS", False)
SLOT_ASSETS_GUILD_ID = safe_int_env("SLOT_ASSETS_GUILD_ID", default=None, allow_blank=True)
SLOT_ASSETS_CHANNEL_ID = safe_int_env("SLOT_ASSETS_CHANNEL_ID", default=None, allow_blank=True)
SLOT_ASSETS_ENABLED = _env_flag("SLOT_ASSETS_ENABLED", True)


def slot_assets_ready() -> bool:
    return bool(SLOT_ASSETS_ENABLED and SLOT_ASSETS_GUILD_ID and SLOT_ASSETS_CHANNEL_ID)

DB_HOST = os.getenv("DB_HOST")
DB_PORT = safe_int_env("DB_PORT", default=6543, allow_blank=True)
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Build DATABASE_URL from individual components if not provided directly
# This supports both Railway (individual vars) and other platforms (single URL)
_raw_db_url = os.getenv("DATABASE_URL")
if _raw_db_url:
    DATABASE_URL = _raw_db_url
elif all([DB_USER, DB_PASSWORD, DB_HOST, DB_NAME]):
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
else:
    DATABASE_URL = None


def _derive_sslmode_from_database_url(database_url: str | None) -> str | None:
    if not database_url:
        return None
    try:
        parsed = urlparse(database_url)
        query = parse_qs(parsed.query or "", keep_blank_values=False)
    except Exception:
        return None
    sslmode_values = query.get("sslmode")
    if not sslmode_values:
        return None
    derived = (sslmode_values[0] or "").strip().lower() or None
    if derived:
        log.info("Derived DB_SSL from DATABASE_URL sslmode=%s", derived)
    return derived


_DB_SSL_ENV = os.getenv("DB_SSL")


def _normalize_db_ssl_mode(raw_value: str | None) -> str:
    value = (raw_value or "").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return "require"
    return value


DB_SSL = _normalize_db_ssl_mode(
    (_DB_SSL_ENV.strip().lower() if _DB_SSL_ENV is not None else "")
    or _derive_sslmode_from_database_url(DATABASE_URL)
    or "disable"
)
DB_SSL_CA_FILE = os.getenv("DB_SSL_CA_FILE")
_ssl_verify_default = DB_SSL in {"verify-ca", "verify-full"}
if DB_SSL in {"allow", "prefer", "require"}:
    _ssl_verify_default = False
DB_SSL_VERIFY = _env_flag("DB_SSL_VERIFY", _ssl_verify_default)
DB_SSL_ALLOW_INSECURE_FALLBACK = _env_flag("DB_SSL_ALLOW_INSECURE_FALLBACK", False)

_SSL_VERIFY_IGNORED_WARNING_LOGGED = False

FERNET_KEY = os.getenv("FERNET_KEY")


DB_ACQUIRE_TIMEOUT = max(safe_int_env("DB_ACQUIRE_TIMEOUT", default=10, allow_blank=True) or 10, 1)
DB_STATEMENT_TIMEOUT_MS = max(
    safe_int_env("DB_STATEMENT_TIMEOUT_MS", default=15000, allow_blank=True) or 15000,
    1,
)


def get_db_ssl_config() -> ssl.SSLContext | None:
    global _SSL_VERIFY_IGNORED_WARNING_LOGGED
    value = (DB_SSL or "").strip().lower()
    if value in {"", "disable", "false", "0", "off", "no"}:
        return None

    ca_file = (DB_SSL_CA_FILE or "").strip()
    if ca_file:
        with open(ca_file, "rb"):
            pass

    if value in {"allow", "prefer", "require"}:
        if DB_SSL_VERIFY and not _SSL_VERIFY_IGNORED_WARNING_LOGGED:
            log.warning(
                "DB_SSL_VERIFY is ignored for sslmode=%s; use verify-ca or verify-full instead.",
                value,
            )
            _SSL_VERIFY_IGNORED_WARNING_LOGGED = True
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if value in {"verify-ca", "verify-full"}:
        cafile = ca_file or (certifi.where() if certifi is not None else None)
        if not cafile:
            raise RuntimeError(
                "DB_SSL is set to verify-ca/verify-full but no CA bundle is available. "
                "Install certifi or set DB_SSL_CA_FILE to a valid PEM CA bundle."
            )
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
        context.check_hostname = value == "verify-full"
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    raise RuntimeError(f"Unsupported DB_SSL value {DB_SSL!r}.")


def get_db_ssl_insecure_fallback_config() -> ssl.SSLContext | None:
    value = (DB_SSL or "").strip().lower()
    if value in {"", "disable", "false", "0", "off", "no"}:
        return None
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


TORN_BASE_URL = "https://api.torn.com/v2"
TORN_API_KEY_LINK = (
    "https://www.torn.com/preferences.php#tab=api"
    "?step=addNewKey&user=basic,discord,bars,cooldowns,log"
    "&title=Happy_Jumper_Bot"
)
TORN_API_DISCLAIMER_URL = "https://bjornodinsson89.github.io/happy-jumper-bot-legal/torn-api/"

DVD_ITEM_ID = 366
XANAX_ITEM_ID = 206
ECSTASY_ITEM_ID = 41

LOG_IDS = {
    "xanax_use": 2290,
    "xanax_overdose": 2291,
    "ecstasy_use": 2286,
    "ecstasy_overdose": 2287,
    "item_sent": 2210,
    "item_received": 2211,
    "cash_sent": 4102,
    "cash_received": 4103,
}

API_RATE_LIMIT_PER_MINUTE = 100
API_RATE_LIMIT_BURST = 10
PAYMENT_VERIFICATION_LOG_LIMIT = 10
DEFAULT_RESERVATION_TIMEOUT = 5
MAX_JUMP_SPOTS = 10
MIN_JUMP_SPOTS = 1
MAX_XANAX_STACK = 3
MIN_XANAX_STACK = 1
MAX_START_DELAY_HOURS = 30
MIN_ENERGY_REQUIREMENT = 250
INSURANCE_MONITOR_INTERVAL = 60
MAX_INSURANCE_DURATION_HOURS = 720
MIN_INSURANCE_DURATION_HOURS = 1
MAX_COVERAGE_XANAX = 1000
MIN_COVERAGE_XANAX = 1
INSURANCE_RESERVATION_TIMEOUT = 10
COVERAGE_TYPES = {
    "xanax": "Xanax",
    "ecstasy_after_stack": "Ecstasy After Stack",
    "all_drugs": "All Drug-Related Losses",
}
RAFFLE_RESERVATION_TIMEOUT = 10
MAX_RAFFLE_TICKETS = 10000
MIN_RAFFLE_TICKETS = 10
MAX_TICKET_PRICE = 100
MIN_TICKET_PRICE = 1
MAX_RAFFLE_DURATION_HOURS = 720
MIN_RAFFLE_DURATION_HOURS = 1
RAFFLE_CHECK_INTERVAL = 60
CLEANUP_INTERVAL = 60
READINESS_REFRESH_INTERVAL = 300
INSURANCE_CHECK_INTERVAL = 60
RAFFLE_COMPLETION_INTERVAL = 60

COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_WARNING = 0xF39C12
COLOR_INFO = 0x3498DB
COLOR_PRIMARY = 0x9B59B6
COLOR_SECONDARY = 0x95A5A6
COLOR_INSURANCE = 0x1ABC9C
COLOR_RAFFLE = 0xE91E63

EMOJI_CHECK = "✅"
EMOJI_CROSS = "❌"
EMOJI_WARNING = "⚠️"
EMOJI_INFO = "ℹ️"
EMOJI_MONEY = "💰"
EMOJI_PILL = "💊"
EMOJI_TICKET = "🎟️"
EMOJI_TROPHY = "🏆"
EMOJI_CLOCK = "⏰"
EMOJI_USER = "👤"
EMOJI_JUMP = "🪂"
EMOJI_SHIELD = "🛡️"
EMOJI_LOCK = "🔒"
EMOJI_UNLOCK = "🔓"
EMOJI_STAR = "⭐"
EMOJI_FIRE = "🔥"
EMOJI_CHART = "📊"
EMOJI_LIST = "📋"
EMOJI_DICE = "🎲"

MIN_RATING = 1
MAX_RATING = 5
REPUTATION_THRESHOLD_GOOD = 4.0
REPUTATION_THRESHOLD_BAD = 2.5

PAYMENT_TYPES = {
    "xanax": {"id": XANAX_ITEM_ID, "name": "Xanax", "emoji": EMOJI_PILL},
    "erotic_dvd": {"id": DVD_ITEM_ID, "name": "Erotic DVD", "emoji": "📀"},
}


def validate_config() -> None:
    required = {
        "DATABASE_URL": DATABASE_URL,
        "FERNET_KEY": FERNET_KEY,
        "DISCORD_TOKEN": DISCORD_TOKEN,
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    if DATABASE_URL:
        parsed = urlparse(DATABASE_URL)
        if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
            log.warning("DATABASE_URL appears malformed (missing expected scheme/host).")

    if FERNET_KEY:
        try:
            from cryptography.fernet import Fernet

            Fernet(FERNET_KEY.encode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "Invalid FERNET_KEY: must be a valid Fernet urlsafe-base64 32-byte key."
            ) from exc
