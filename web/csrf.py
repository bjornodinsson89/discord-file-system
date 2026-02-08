"""CSRF helpers for cookie-backed dashboard sessions."""

from __future__ import annotations

import secrets
from fastapi import Request, HTTPException

CSRF_SESSION_KEY = "csrf_token"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_or_create_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def enforce_csrf(request: Request) -> None:
    """Validate CSRF token for state-changing /api requests using session auth."""
    if request.method in SAFE_METHODS:
        return
    if not request.url.path.startswith("/api/"):
        return

    # Only enforce for cookie-session authenticated users.
    if not request.session.get("user"):
        return

    expected = get_or_create_csrf_token(request)
    provided = request.headers.get(CSRF_HEADER)
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
