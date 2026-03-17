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


def test_collect_rows_uses_live_helper_not_snapshot_path(monkeypatch):
    async def _run():
        class _Member:
            def __init__(self, mid, name, bot=False):
                self.id = mid
                self.display_name = name
                self.bot = bot

        class _Role:
            def __init__(self, members):
                self.members = members

        guild = SimpleNamespace(
            id=99,
            get_role=lambda _rid: _Role([_Member(11, "Host A"), _Member(12, "Bot", bot=True)]),
        )

        calls = {"live": 0}

        async def fake_live(*, users_repo, discord_id, guild_id):
            calls["live"] += 1
            return {
                "has_api_key": True,
                "torn_name": "Alpha",
                "torn_user_id": 101,
                "energy": 1000,
                "energy_max": 1000,
                "drug_cooldown": 0,
                "booster_cooldown": 0,
                "status_text": "ok",
            }

        async def forbidden_snapshot(**_kwargs):
            raise AssertionError("snapshot helper must not be used for who-can-jump")

        monkeypatch.setattr(events, "_fetch_who_can_jump_readiness", fake_live)
        monkeypatch.setattr(events, "_fetch_and_upsert_user_readiness_snapshot", forbidden_snapshot)

        rows, state = await events._collect_who_can_jump_rows(
            guild=guild,
            settings={"host99k_role_id": 1},
            users_repo=object(),
        )
        assert state is None
        assert calls["live"] == 1
        assert len(rows) == 1
        assert rows[0]["status"] == "Ready Now"

    asyncio.run(_run())


def test_fetch_who_can_jump_readiness_success_and_no_upsert(monkeypatch):
    async def _run():
        class _UsersRepo:
            async def get_user_api_key(self, _discord_id):
                return {"encrypted_key": "enc", "torn_name": "Stored", "torn_user_id": 123}

        class _Security:
            def decrypt_api_key(self, value):
                assert value == "enc"
                return "plain"

        class _Torn:
            async def get_user_data(self, *_args, **_kwargs):
                return {
                    "profile": {"id": 999, "name": "LiveName"},
                    "bars": {"energy": {"current": 1200, "maximum": 1500}},
                    "cooldowns": {"drug": 0, "booster": 15},
                }

        async def forbidden_upsert(**_kwargs):
            raise AssertionError("upsert_readiness_snapshot should not be called")

        monkeypatch.setattr(events, "get_security_manager", lambda: _Security())
        monkeypatch.setattr(events, "get_torn_api", lambda: _Torn())
        monkeypatch.setattr(events.JumpsRepository, "upsert_readiness_snapshot", forbidden_upsert)

        payload = await events._fetch_who_can_jump_readiness(
            users_repo=_UsersRepo(),
            discord_id=1,
            guild_id=5,
        )
        assert payload["has_api_key"] is True
        assert payload["torn_name"] == "LiveName"
        assert payload["torn_user_id"] == 999
        assert payload["energy"] == 1200
        assert payload["energy_max"] == 1500
        assert payload["drug_cooldown"] == 0
        assert payload["booster_cooldown"] == 15

    asyncio.run(_run())


def test_fetch_who_can_jump_readiness_error_mapping(monkeypatch):
    async def _run():
        class _UsersRepo:
            def __init__(self, row):
                self.row = row

            async def get_user_api_key(self, _discord_id):
                return self.row

        class _Security:
            def __init__(self, should_fail=False):
                self.should_fail = should_fail

            def decrypt_api_key(self, _value):
                if self.should_fail:
                    raise RuntimeError("decrypt failed")
                return "plain"

        class _TornPerm:
            async def get_user_data(self, *_args, **_kwargs):
                raise events.TornAPIPermissionError("missing permissions")

        class _TornRate:
            async def get_user_data(self, *_args, **_kwargs):
                raise events.TornAPIRateLimitError("rate limited")

        missing = await events._fetch_who_can_jump_readiness(
            users_repo=_UsersRepo(None), discord_id=1, guild_id=1
        )
        assert missing["status_text"] == "API key missing"
        assert missing["has_api_key"] is False

        monkeypatch.setattr(events, "get_security_manager", lambda: _Security())
        monkeypatch.setattr(events, "get_torn_api", lambda: _TornPerm())
        perm = await events._fetch_who_can_jump_readiness(
            users_repo=_UsersRepo({"encrypted_key": "enc"}),
            discord_id=1,
            guild_id=1,
        )
        assert perm["status_text"] == "API permissions missing"

        monkeypatch.setattr(events, "get_torn_api", lambda: _TornRate())
        unavailable = await events._fetch_who_can_jump_readiness(
            users_repo=_UsersRepo({"encrypted_key": "enc"}),
            discord_id=1,
            guild_id=1,
        )
        assert unavailable["status_text"] == "API unavailable"

        monkeypatch.setattr(events, "get_security_manager", lambda: _Security(should_fail=True))
        decrypt_fail = await events._fetch_who_can_jump_readiness(
            users_repo=_UsersRepo({"encrypted_key": "enc"}),
            discord_id=1,
            guild_id=1,
        )
        assert decrypt_fail["status_text"] == "API unavailable"

    asyncio.run(_run())


def test_refresh_guard_blocks_overlapping_refreshes(monkeypatch):
    async def _run():
        events._WHO_CAN_JUMP_REFRESH_LOCKS.clear()
        started = asyncio.Event()
        unblock = asyncio.Event()
        calls = {"count": 0}

        async def fake_locked(guild_id, *, requested_page_index=None):
            calls["count"] += 1
            started.set()
            await unblock.wait()

        monkeypatch.setattr(events, "_refresh_who_can_jump_panel_for_guild_locked", fake_locked)

        first = asyncio.create_task(events._refresh_who_can_jump_panel_for_guild(77))
        await started.wait()
        await events._refresh_who_can_jump_panel_for_guild(77)
        unblock.set()
        await first
        assert calls["count"] == 1

    asyncio.run(_run())
