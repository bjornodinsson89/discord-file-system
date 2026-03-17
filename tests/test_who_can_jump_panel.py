import asyncio
from types import SimpleNamespace

import discord

from cogs import events


def _row(name: str) -> dict:
    return {
        "identity": name,
        "status": "Ready Now",
        "energy": 1000,
        "drug_cooldown": 0,
        "booster_cooldown": 0,
    }


def test_progress_bars_and_duration_formatting():
    assert events._progress_bar(1000, 1000) == "[██████████]"
    assert events._render_energy_bar(650).endswith("650/1000")
    assert events._render_cooldown_bar(0).endswith("Ready")
    assert events._render_cooldown_bar(None).endswith("Unknown")
    assert events._format_duration_short(3900) == "1h 05m left"


def test_identity_fallback_never_uses_raw_discord_id():
    identity = events._format_who_can_jump_identity(
        torn_name=None,
        torn_user_id=None,
        fallback_name="DisplayName",
    )
    assert identity == "DisplayName"
    assert "User <" not in identity


def test_ten_or_fewer_hosts_is_single_page():
    embed, total_pages, page_index = events._build_who_can_jump_embed(
        rows=[_row(f"Host {i}") for i in range(10)],
        state=None,
        page_index=0,
        page_size=10,
    )
    assert total_pages == 1
    assert page_index == 0
    assert embed.footer.text == "Page 1/1"


def test_eleven_hosts_is_two_pages():
    embed, total_pages, page_index = events._build_who_can_jump_embed(
        rows=[_row(f"Host {i}") for i in range(11)],
        state=None,
        page_index=1,
        page_size=10,
    )
    assert total_pages == 2
    assert page_index == 1
    assert embed.footer.text == "Page 2/2"


def test_invalid_page_index_clamps_to_last_page():
    embed, total_pages, page_index = events._build_who_can_jump_embed(
        rows=[_row(f"Host {i}") for i in range(11)],
        state=None,
        page_index=99,
        page_size=10,
    )
    assert total_pages == 2
    assert page_index == 1
    assert embed.footer.text == "Page 2/2"


def test_buttons_disable_correctly_at_edges():
    async def _run():
        first = events.WhoCanJumpPanelView(guild_id=1, page_index=0, total_pages=3)
        last = events.WhoCanJumpPanelView(guild_id=1, page_index=2, total_pages=3)
        single = events.WhoCanJumpPanelView(guild_id=1, page_index=0, total_pages=1)

        first_prev, first_next, _ = first.children
        last_prev, last_next, _ = last.children
        one_prev, one_next, _ = single.children

        assert first_prev.disabled is True
        assert first_next.disabled is False
        assert last_prev.disabled is False
        assert last_next.disabled is True
        assert one_prev.disabled is True
        assert one_next.disabled is True

    asyncio.run(_run())


def test_next_prev_and_refresh_callbacks_use_expected_page(monkeypatch):
    async def _run():
        called = []

        async def fake_refresh(guild_id, *, requested_page_index=None):
            called.append((guild_id, requested_page_index))

        monkeypatch.setattr(events, "_refresh_who_can_jump_panel_for_guild", fake_refresh)

        class _Resp:
            async def defer(self):
                return None

        interaction = SimpleNamespace(response=_Resp())
        view = events.WhoCanJumpPanelView(guild_id=7, page_index=1, total_pages=5)

        await view._on_prev(interaction)
        await view._on_next(interaction)
        await view._on_refresh(interaction)

        assert called == [(7, 0), (7, 2), (7, 1)]

    asyncio.run(_run())


def test_refresh_preserves_current_page_and_recreates_deleted_message(monkeypatch):
    async def _run():
        class FakeNotFound(discord.NotFound):
            def __init__(self):
                super().__init__(SimpleNamespace(status=404, reason="not found"), "gone")

        class FirstMessage:
            async def edit(self, **_kwargs):
                raise FakeNotFound()

        edits = {"count": 0}

        class SecondMessage:
            async def edit(self, **kwargs):
                edits["count"] += 1
                self.kwargs = kwargs

        fake_guild = SimpleNamespace(id=42)
        monkeypatch.setattr(events.bot, "get_guild", lambda _gid: fake_guild)

        class FakeSettingsRepo:
            def __init__(self, _db):
                self.settings = {
                    "who_can_jump_channel_id": 1,
                    "who_can_jump_message_id": 2,
                    "who_can_jump_page_index": 1,
                    "host99k_role_id": 1,
                }

            async def get_settings(self, _gid):
                return dict(self.settings)

            async def upsert_settings(self, _gid, **fields):
                self.settings.update(fields)

        monkeypatch.setattr(events, "GuildSettingsRepository", FakeSettingsRepo)
        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(events, "UsersRepository", lambda _pool: object())
        monkeypatch.setattr(events, "JumpsRepository", lambda _pool: object())

        calls = {"count": 0}

        async def fake_ensure(**_kwargs):
            calls["count"] += 1
            return FirstMessage() if calls["count"] == 1 else SecondMessage()

        async def fake_collect(**_kwargs):
            return ([_row(f"Host {i}") for i in range(11)], None)

        monkeypatch.setattr(events, "_ensure_who_can_jump_panel_message", fake_ensure)
        monkeypatch.setattr(events, "_collect_who_can_jump_rows", fake_collect)

        await events._refresh_who_can_jump_panel_for_guild(42)

        assert calls["count"] == 2
        assert edits["count"] == 1

    asyncio.run(_run())


def test_who_can_jump_worker_continues_when_one_guild_fails(monkeypatch):
    async def _run():
        class _Conn:
            async def fetch(self, _query):
                return [{"guild_id": 1}, {"guild_id": 2}]

        class _Acquire:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _Pool:
            def acquire(self):
                return _Acquire()

        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=_Pool()))

        async def fake_db_ready(_name):
            return True

        monkeypatch.setattr(events, "_worker_db_ready", fake_db_ready)

        called = []

        async def fake_refresh(guild_id, *, requested_page_index=None):
            called.append(guild_id)
            if guild_id == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(events, "_refresh_who_can_jump_panel_for_guild", fake_refresh)

        await events.who_can_jump_panel_worker.coro()
        assert called == [1, 2]

    asyncio.run(_run())
