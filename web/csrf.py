"""CSRF helpers for cookie-backed dashboard sessions."""

from __future__ import annotations

import logging
import base64
import hashlib
import hmac
import secrets
from fastapi import Request, HTTPException

CSRF_HEADER = "x-csrf-token"
CSRF_COOKIE = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}

log = logging.getLogger("happy_jumper.csrf")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def generate_csrf_token(secret: str) -> str:
    """Generate signed double-submit token: b64(nonce).b64(hmac(nonce))."""
    nonce = secrets.token_bytes(32)
    signature = hmac.new(secret.encode("utf-8"), nonce, hashlib.sha256).digest()
    return f"{_b64url_encode(nonce)}.{_b64url_encode(signature)}"


def verify_csrf_token(token: str, secret: str) -> bool:
    """Verify token signature without relying on server-side session state."""
    if not token or "." not in token:
        return False

    nonce_part, sig_part = token.split(".", 1)
    try:
        nonce = _b64url_decode(nonce_part)
        signature = _b64url_decode(sig_part)
    except Exception:
        return False

    if len(nonce) != 32 or len(signature) != hashlib.sha256().digest_size:
        return False

    expected = hmac.new(secret.encode("utf-8"), nonce, hashlib.sha256).digest()
    return hmac.compare_digest(signature, expected)


async def enforce_csrf(request: Request) -> None:
    """Validate CSRF token for state-changing /api requests using session auth."""
    if request.method in SAFE_METHODS:
        return
    if not request.url.path.startswith("/api/"):
        return

    # Only enforce for cookie-session authenticated users.
    if not request.session.get("user"):
        return

    provided = request.headers.get(CSRF_HEADER)
    cookie_token = request.cookies.get(CSRF_COOKIE)

    if not provided:
        raise HTTPException(status_code=403, detail={"detail": "Invalid CSRF token", "code": "csrf_invalid", "reason": "missing_header"})

    if not cookie_token:
        raise HTTPException(status_code=403, detail={"detail": "Invalid CSRF token", "code": "csrf_invalid", "reason": "missing_cookie"})

    if not secrets.compare_digest(provided, cookie_token):
        raise HTTPException(status_code=403, detail={"detail": "Invalid CSRF token", "code": "csrf_invalid", "reason": "mismatch"})

    secret = getattr(request.app.state, "csrf_hmac_secret", None)
    if not secret:
        raise HTTPException(status_code=500, detail="CSRF secret is not configured")

    if not verify_csrf_token(provided, secret):
        raise HTTPException(status_code=403, detail={"detail": "Invalid CSRF token", "code": "csrf_invalid", "reason": "bad_signature"})
