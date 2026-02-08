import asyncio
import importlib

import admin_api.handlers as handlers
import config
from admin_api.schemas import CreateSessionRequest
from web import csrf


class _DummyRequest:
    def __init__(self, method: str, path: str, session: dict | None = None, headers: dict | None = None):
        self.method = method
        self.url = type("URL", (), {"path": path})()
        self.session = session if session is not None else {}
        self.headers = headers or {}


class _DummyTornAPI:
    async def get_torn_time(self):
        return 1000


class _DummyDB:
    def __init__(self):
        self.session = {
            "id": 77,
            "guild_id": 1,
            "host_discord_id": 123,
            "host_torn_id": 999,
            "max_spots": 5,
            "xanax_stack": "1_xanax",
            "payment_type": "xanax",
            "payment_amount": 2,
            "status": "open",
            "announcement_message_id": None,
            "announcement_channel_id": None,
            "created_at": "2024-01-01T00:00:00+00:00",
            "created_by_dashboard": True,
            "dashboard_admin_id": 123,
        }

    async def get_user_api_key(self, discord_id):
        return {"torn_user_id": 999}

    async def create_jump_session(self, **kwargs):
        return 77

    async def update_jump_session(self, session_id, **kwargs):
        self.session.update(kwargs)

    async def get_jump_session(self, session_id):
        return dict(self.session)

    async def log_audit(self, *args, **kwargs):
        return None

    async def get_session_signups(self, session_id):
        return []

    async def get_guild_settings(self, guild_id):
        return {"jump_99k_channel_id": 2}


def test_web_imports_without_discord_runtime(monkeypatch):
    monkeypatch.setenv("SERVICE_MODE", "WEB")
    importlib.reload(config)
    importlib.reload(handlers)

    from web.app import health_check

    health = asyncio.run(health_check())
    assert health["status"] == "healthy"


def test_create_session_in_web_mode_uses_rest_send_message(monkeypatch):
    monkeypatch.setenv("SERVICE_MODE", "WEB")
    importlib.reload(config)
    importlib.reload(handlers)

    sent = {}

    class DummyRestClient:
        async def send_message(self, channel_id, **kwargs):
            sent["channel_id"] = channel_id
            sent["kwargs"] = kwargs
            return {"id": "555"}

    db = _DummyDB()

    async def run():
        monkeypatch.setattr(handlers, "DiscordRestClient", DummyRestClient)
        monkeypatch.setattr(handlers, "_validate_discord_channel", lambda guild_id, channel_id: asyncio.sleep(0))
        monkeypatch.setattr(handlers, "get_database", lambda: db)
        monkeypatch.setattr(handlers, "get_torn_api", lambda: _DummyTornAPI())

        request = CreateSessionRequest(
            guild_id=1,
            channel_id=2,
            payment_type="xanax",
            payment_amount=2,
            spots=5,
            xanax_stack="1_xanax",
            start_delay_hours=0,
        )
        response = await handlers.create_session_handler(request, 123)
        assert response.announcement_message_id == 555

    asyncio.run(run())
    assert sent["channel_id"] == 2


def test_update_session_in_web_mode_uses_rest_edit_message(monkeypatch):
    monkeypatch.setenv("SERVICE_MODE", "WEB")
    importlib.reload(config)
    importlib.reload(handlers)

    edited = {}

    class DummyRestClient:
        async def edit_message(self, channel_id, message_id, **kwargs):
            edited["channel_id"] = channel_id
            edited["message_id"] = message_id
            edited["kwargs"] = kwargs
            return {"id": str(message_id)}

    db = _DummyDB()
    db.session["announcement_message_id"] = 555
    db.session["announcement_channel_id"] = 2

    async def run():
        monkeypatch.setattr(handlers, "DiscordRestClient", DummyRestClient)
        monkeypatch.setattr(handlers, "get_database", lambda: db)
        await handlers.update_session_message(77)

    asyncio.run(run())
    assert edited["channel_id"] == 2
    assert edited["message_id"] == 555


def test_csrf_safe_methods_are_allowed():
    request = _DummyRequest("GET", "/api/sessions/list", session={"user": {"id": "1"}})
    asyncio.run(csrf.enforce_csrf(request))


def test_csrf_rejects_unsafe_method_without_header():
    request = _DummyRequest("POST", "/api/sessions/create", session={"user": {"id": "1"}})

    import pytest

    with pytest.raises(Exception, match="Invalid CSRF token"):
        asyncio.run(csrf.enforce_csrf(request))


def test_csrf_accepts_matching_token_header():
    request = _DummyRequest("POST", "/api/settings/update", session={"user": {"id": "1"}})
    token = csrf.get_or_create_csrf_token(request)
    request.headers[csrf.CSRF_HEADER] = token

    asyncio.run(csrf.enforce_csrf(request))
