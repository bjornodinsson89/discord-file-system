import asyncio

import bot_actions.handlers as handlers
import cogs.events as events


class _DummyDB:
    pool = object()


def test_list_sessions_page_flooring(monkeypatch):
    offsets = []

    async def _fake_list_sessions(self, guild_id, status, limit, offset):
        offsets.append(offset)
        return []

    monkeypatch.setattr(
        handlers.JumpsRepository, "list_sessions", _fake_list_sessions, raising=False
    )
    monkeypatch.setattr("utils.get_database", lambda: _DummyDB())

    async def _run():
        await handlers.list_sessions_handler(guild_id=1, status=None, page=1, per_page=25)
        await handlers.list_sessions_handler(guild_id=1, status=None, page=0, per_page=25)
        await handlers.list_sessions_handler(guild_id=1, status=None, page=-1, per_page=25)

    asyncio.run(_run())

    assert offsets == [0, 0, 0]


def test_list_raffles_page_flooring(monkeypatch):
    offsets = []

    async def _fake_list_raffles(self, guild_id, status, limit, offset):
        offsets.append(offset)
        return []

    monkeypatch.setattr(
        handlers.RafflesRepository, "list_raffles", _fake_list_raffles, raising=False
    )
    monkeypatch.setattr("utils.get_database", lambda: _DummyDB())

    async def _run():
        await handlers.list_raffles_handler(guild_id=1, status=None, page=1, per_page=20)
        await handlers.list_raffles_handler(guild_id=1, status=None, page=0, per_page=20)
        await handlers.list_raffles_handler(guild_id=1, status=None, page=-1, per_page=20)

    asyncio.run(_run())

    assert offsets == [0, 0, 0]


def test_my_sessions_uses_join_query_helper_once(monkeypatch):
    calls = {"joined": 0}

    async def _fake_joined(self, *, guild_id, user_id):
        calls["joined"] += 1
        return [
            {
                "id": 42,
                "status": "open",
                "host_discord_id": user_id,
                "xanax_count": 3,
                "max_spots": 5,
                "user_signup_status": "reserved",
            }
        ]

    async def _should_not_call(*_args, **_kwargs):
        raise AssertionError("N+1 path should not be called")

    monkeypatch.setattr(events, "get_database", lambda: _DummyDB())
    monkeypatch.setattr(events.JumpsRepository, "list_open_sessions_with_user_signup", _fake_joined)
    monkeypatch.setattr(events.JumpsRepository, "get_signup", _should_not_call)

    class _Response:
        async def defer(self, ephemeral=True):
            return None

    class _Followup:
        def __init__(self):
            self.sent = 0

        async def send(self, *, embed, ephemeral=True):
            self.sent += 1
            return None

    class _User:
        id = 100

    class _Interaction:
        guild_id = 999
        user = _User()
        response = _Response()

        def __init__(self):
            self.followup = _Followup()

    interaction = _Interaction()
    asyncio.run(events.my_sessions.callback(interaction))

    assert calls["joined"] == 1
    assert interaction.followup.sent == 1
