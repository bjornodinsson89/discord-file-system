import asyncio
import importlib

import pytest
from fastapi import HTTPException

import admin_api.routes as admin_routes
import config
from admin_api.schemas import CreateSessionRequest
from web import csrf


class _DummyRequest:
    def __init__(self, method: str, path: str, session: dict | None = None, headers: dict | None = None):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self.session = session if session is not None else {}
        self.headers = headers or {}


def test_web_app_health_and_admin_endpoint_without_bot(monkeypatch):
    monkeypatch.setenv("SERVICE_MODE", "WEB")
    importlib.reload(config)

    import admin_api.handlers as handlers
    from web.app import health_check

    async def fake_require_guild_admin(guild_id: int, user: dict):
        return user

    class DummyDB:
        pass

    monkeypatch.setattr(admin_routes, "require_guild_admin", fake_require_guild_admin)
    monkeypatch.setattr(handlers, "get_database", lambda: DummyDB())

    request = CreateSessionRequest(
        guild_id=1,
        channel_id=2,
        payment_type="xanax",
        payment_amount=1,
        spots=1,
        xanax_stack="1_xanax",
        start_delay_hours=0,
    )
    user = {"id": "123", "guilds": [{"id": "1", "permissions": str(8)}]}

    health = asyncio.run(health_check())
    assert health["status"] == "healthy"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin_routes.create_session(request, user))

    assert exc.value.status_code == 503
    assert "Bot runtime is unavailable" in str(exc.value.detail)


def test_csrf_safe_methods_are_allowed():
    request = _DummyRequest("GET", "/api/sessions/list", session={"user": {"id": "1"}})
    asyncio.run(csrf.enforce_csrf(request))


def test_csrf_rejects_unsafe_method_without_header():
    request = _DummyRequest("POST", "/api/sessions/create", session={"user": {"id": "1"}})

    with pytest.raises(HTTPException, match="Invalid CSRF token"):
        asyncio.run(csrf.enforce_csrf(request))


def test_csrf_accepts_matching_token_header():
    session = {"user": {"id": "1"}}
    request = _DummyRequest("POST", "/api/settings/update", session=session)
    token = csrf.get_or_create_csrf_token(request)
    request.headers[csrf.CSRF_HEADER] = token

    asyncio.run(csrf.enforce_csrf(request))
