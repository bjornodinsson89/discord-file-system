from __future__ import annotations

import logging
import ssl
from typing import Optional

import asyncpg

import config
from utils.structured_log import log_event

log = logging.getLogger("happy_jumper.repositories")


class RepositoryBase:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool


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

    log.info(
        "Initializing DB pool (ssl_mode=%s, using_dsn=%s)",
        ssl_mode_name,
        bool(db_url),
    )
    if ssl_mode_name not in {"", "disable", "false", "0", "off", "no"} and not verify_enabled:
        log.warning("DB SSL is enabled while certificate verification is disabled (DB_SSL_VERIFY=false)")

    base_pool_kwargs = {
        "min_size": 2,
        "max_size": 10,
        "command_timeout": 60,
    }

    async def _build_pool(ssl_context) -> asyncpg.Pool:
        pool_kwargs = {**base_pool_kwargs, "ssl": ssl_context}
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
            "If using a managed DB with valid CA, set DB_SSL_VERIFY=true and do not disable verify. "
            "If your provider uses a custom/self-signed CA, set DB_SSL_CA_FILE to the CA bundle. "
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
