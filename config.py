"""
Happy Jumper Bot Configuration - vNext
Extended with Dashboard, OAuth, and Admin API settings.
"""

import os

# ============================================================================
# DISCORD CONFIGURATION
# ============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

# ============================================================================
# DASHBOARD CONFIGURATION
# ============================================================================
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", DASHBOARD_URL).rstrip("/")

# IMPORTANT:
# - Always define this before using it anywhere else (prevents NameError).
# - In production you MUST set DASHBOARD_SECRET_KEY (validate_config enforces it).
DASHBOARD_SECRET_KEY_ENV = os.getenv("DASHBOARD_SECRET_KEY")
DASHBOARD_SECRET_KEY = DASHBOARD_SECRET_KEY_ENV or os.urandom(32).hex()

# OAuth redirect URI should ideally be explicit via env to avoid path mismatches.
# If not provided, fall back to the expected mounted auth router location.
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI",
    f"{DASHBOARD_URL}/auth/callback"
)

# ============================================================================
# DATABASE CONFIGURATION
# ============================================================================
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "6543"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSL = os.getenv("DB_SSL", "require")
RUN_MIGRATIONS_ON_STARTUP = os.getenv("RUN_MIGRATIONS_ON_STARTUP", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ============================================================================
# SECURITY
# ============================================================================
FERNET_KEY = os.getenv("FERNET_KEY")
SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE")
SESSION_COOKIE_SECURE_ENV = os.getenv("SESSION_COOKIE_SECURE")


def _is_https_url(url: str) -> bool:
    return url.lower().startswith("https://")


def _env_to_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


if SESSION_COOKIE_SAMESITE is None:
    SESSION_COOKIE_SAMESITE = "none" if _is_https_url(DASHBOARD_URL) else "lax"
else:
    SESSION_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE.strip().lower()

SESSION_COOKIE_SECURE = _env_to_bool(
    SESSION_COOKIE_SECURE_ENV,
    _is_https_url(DASHBOARD_URL)
)

# Browsers reject SameSite=None cookies unless Secure is enabled.
if SESSION_COOKIE_SAMESITE == "none":
    SESSION_COOKIE_SECURE = True

# ============================================================================
# TORN API
# ============================================================================
# Torn API v2 base URL (v1 endpoints are not permitted).
TORN_BASE_URL = "https://api.torn.com/v2"
TORN_API_KEY_LINK = (
    "https://www.torn.com/preferences.php#tab=api"
    "?step=addNewKey&user=basic,discord,bars,cooldowns,log"
    "&title=Happy_Jumper_Bot"
)

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

# ============================================================================
# HAPPY JUMP SETTINGS
# ============================================================================
DEFAULT_RESERVATION_TIMEOUT = 5
MAX_JUMP_SPOTS = 10
MIN_JUMP_SPOTS = 1
MAX_XANAX_STACK = 3
MIN_XANAX_STACK = 1
MAX_START_DELAY_HOURS = 30
MIN_ENERGY_REQUIREMENT = 250

# ============================================================================
# INSURANCE SETTINGS
# ============================================================================
INSURANCE_MONITOR_INTERVAL = 60
MAX_INSURANCE_DURATION_HOURS = 720  # 30 days
MIN_INSURANCE_DURATION_HOURS = 1
MAX_COVERAGE_XANAX = 1000
MIN_COVERAGE_XANAX = 1
INSURANCE_RESERVATION_TIMEOUT = 10

COVERAGE_TYPES = {
    "xanax_stack": "Xanax Stack Only",
    "ecstasy_after_stack": "Ecstasy After Stack",
    "all_drugs": "All Drug-Related Losses"
}

# ============================================================================
# RAFFLE SETTINGS
# ============================================================================
RAFFLE_RESERVATION_TIMEOUT = 10
MAX_RAFFLE_TICKETS = 10000
MIN_RAFFLE_TICKETS = 10
MAX_TICKET_PRICE = 100
MIN_TICKET_PRICE = 1
MAX_RAFFLE_DURATION_HOURS = 720  # 30 days
MIN_RAFFLE_DURATION_HOURS = 1
RAFFLE_CHECK_INTERVAL = 60

# ============================================================================
# WORKER INTERVALS (seconds)
# ============================================================================
CLEANUP_INTERVAL = 60
READINESS_REFRESH_INTERVAL = 300
INSURANCE_CHECK_INTERVAL = 60
RAFFLE_COMPLETION_INTERVAL = 60

# ============================================================================
# UI - COLORS
# ============================================================================
COLOR_SUCCESS = 0x2ECC71
COLOR_ERROR = 0xE74C3C
COLOR_WARNING = 0xF39C12
COLOR_INFO = 0x3498DB
COLOR_PRIMARY = 0x9B59B6
COLOR_SECONDARY = 0x95A5A6
COLOR_INSURANCE = 0x1ABC9C
COLOR_RAFFLE = 0xE91E63

# ============================================================================
# UI - EMOJIS
# ============================================================================
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

# ============================================================================
# HOST REPUTATION
# ============================================================================
MIN_RATING = 1
MAX_RATING = 5
REPUTATION_THRESHOLD_GOOD = 4.0
REPUTATION_THRESHOLD_BAD = 2.5

# ============================================================================
# PAYMENT TYPES
# ============================================================================
PAYMENT_TYPES = {
    "xanax": {"id": XANAX_ITEM_ID, "name": "Xanax", "emoji": EMOJI_PILL},
    "erotic_dvd": {"id": DVD_ITEM_ID, "name": "Erotic DVD", "emoji": "📀"}
}

# ============================================================================
# VALIDATION
# ============================================================================
def validate_config() -> None:
    """Validate required environment variables."""
    required = {
        "DISCORD_TOKEN": DISCORD_TOKEN,
        "DISCORD_CLIENT_ID": DISCORD_CLIENT_ID,
        "DISCORD_CLIENT_SECRET": DISCORD_CLIENT_SECRET,
        "DASHBOARD_SECRET_KEY": DASHBOARD_SECRET_KEY_ENV,
        "DB_HOST": DB_HOST,
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "FERNET_KEY": FERNET_KEY,
    }

    missing = [name for name, value in required.items() if not value]

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
