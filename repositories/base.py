from __future__ import annotations

import logging
import ssl
from contextlib import asynccontextmanager
from typing import AsyncIterator
from typing import Optional

import asyncpg

import config
from utils.db_acquire import acquire_conn
from utils.structured_log import log_event

log = logging.getLogger("happy_jumper.repositories")


class RepositoryBase:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        async with acquire_conn(self.pool, config.DB_ACQUIRE_TIMEOUT) as conn:
            yield conn


def pool_is_open(pool: Optional[asyncpg.Pool]) -> bool:
    if pool is None:
        return False
    is_closing = getattr(pool, "is_closing", None)
    if callable(is_closing):
        return not bool(is_closing())
    closed = getattr(pool, "_closed", None)
    if closed is not None:
        return not bool(closed)
    return True


def _find_ssl_cert_error(exc: BaseException) -> ssl.SSLCertVerificationError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return current
        current = current.__cause__ or current.__context__
    return None


async def create_pool() -> asyncpg.Pool:
    ssl_mode = config.get_db_ssl_config()
    db_url = (config.DATABASE_URL or "").strip() or None
    ssl_mode_name = (config.DB_SSL or "disable").strip().lower()
    verify_enabled = bool(getattr(config, "DB_SSL_VERIFY", True))
    ca_file_present = bool((getattr(config, "DB_SSL_CA_FILE", None) or "").strip())
    db_host = (config.DB_HOST or "").strip() or None

    log.info(
        "Initializing DB pool (ssl_mode=%s, using_dsn=%s)",
        ssl_mode_name,
        bool(db_url),
    )
    if ssl_mode_name not in {"", "disable", "false", "0", "off", "no"} and not verify_enabled:
        log.warning(
            "DB SSL is enabled while certificate verification is disabled (DB_SSL_VERIFY=false)"
        )

    base_pool_kwargs = {
        "min_size": config.DB_POOL_MIN_SIZE,
        "max_size": config.DB_POOL_MAX_SIZE,
        "max_inactive_connection_lifetime": config.DB_POOL_MAX_INACTIVE_LIFETIME,
        "command_timeout": 60,
    }

    async def _init_connection(conn: asyncpg.Connection) -> None:
        statement_timeout_ms = int(getattr(config, "DB_STATEMENT_TIMEOUT_MS", 15000) or 15000)
        await conn.execute(f"SET statement_timeout = '{statement_timeout_ms}ms'")

    async def _build_pool(ssl_context) -> asyncpg.Pool:
        pool_kwargs = dict(base_pool_kwargs)
        pool_kwargs["init"] = _init_connection
        if ssl_context is not None:
            pool_kwargs["ssl"] = ssl_context
        if db_url:
            return await asyncpg.create_pool(dsn=db_url, **pool_kwargs)
        return await asyncpg.create_pool(
            host=config.DB_HOST,
            port=config.DB_PORT,
            database=config.DB_NAME,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            **pool_kwargs,
        )

    try:
        return await _build_pool(ssl_mode)
    except Exception as exc:
        cert_exc = _find_ssl_cert_error(exc)
        if cert_exc is None:
            raise
        hint = (
            "TLS certificate verification failed. Ensure CA certificates are installed in the runtime image "
            "or set DB_SSL_CA_FILE to a valid PEM bundle. "
            "If using a managed DB with valid CA, keep DB_SSL_VERIFY=true. "
            "If you accept insecure fallback for non-prod, set DB_SSL_ALLOW_INSECURE_FALLBACK=true or DB_SSL_VERIFY=false."
        )
        log_event(
            log,
            logging.ERROR,
            "db_ssl_verification_failed",
            action="db_connect",
            result="error",
            error_type=type(cert_exc).__name__,
            ssl_mode=ssl_mode_name,
            verify_enabled=verify_enabled,
            ca_file_present=ca_file_present,
            host=db_host,
            hint=hint,
            exc_info=True,
        )
        if not bool(getattr(config, "DB_SSL_ALLOW_INSECURE_FALLBACK", False)):
            raise
        log_event(
            log,
            logging.WARNING,
            "db_ssl_insecure_fallback_enabled",
            action="db_connect",
            result="warning",
            ssl_mode=ssl_mode_name,
            verify_enabled=False,
        )
        insecure_ssl = config.get_db_ssl_insecure_fallback_config()
        return await _build_pool(insecure_ssl)
