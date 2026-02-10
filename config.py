"""Happy Jumper bot configuration (Discord-only service)."""

import os
import ssl


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0")) or None
CLEAN_COMMANDS = _env_flag("CLEAN_COMMANDS", False)

DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT", "6543"))
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSL = os.getenv("DB_SSL", "disable")
DB_SSL_CA_FILE = os.getenv("DB_SSL_CA_FILE")

FERNET_KEY = os.getenv("FERNET_KEY")


def get_db_ssl_config() -> ssl.SSLContext | None:
    value = (DB_SSL or "").strip().lower()
    if value in {"", "disable", "false", "0", "off", "no"}:
        return None

    ca_file = (DB_SSL_CA_FILE or "").strip()
    if ca_file:
        with open(ca_file, "rb"):
            pass

    if value in {"allow", "prefer", "require", "true", "1", "on", "yes"}:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if value in {"verify-ca", "verify-full"}:
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file or None)
        context.check_hostname = value == "verify-full"
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    raise RuntimeError(f"Unsupported DB_SSL value {DB_SSL!r}.")


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
    "all_drugs": "All Drug-Related Losses"
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
    "erotic_dvd": {"id": DVD_ITEM_ID, "name": "Erotic DVD", "emoji": "📀"}
}


def validate_config() -> None:
    required = {
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "DB_SSL": DB_SSL,
        "FERNET_KEY": FERNET_KEY,
        "DISCORD_TOKEN": DISCORD_TOKEN,
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
