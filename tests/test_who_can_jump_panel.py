import asyncio
from types import SimpleNamespace

import discord

from cogs import events


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


def test_build_who_can_jump_embeds_setup_needed_and_empty_states():
    guild = SimpleNamespace(id=1)
    setup_embeds = events._build_who_can_jump_embeds(guild=guild, rows=[], state="setup_needed")
    empty_embeds = events._build_who_can_jump_embeds(guild=guild, rows=[], state=None)
    assert "Who Can Jump" in (setup_embeds[0].title or "")
    assert "Setup needed" in (setup_embeds[0].description or "")
    assert "No non-bot members" in (empty_embeds[0].description or "")


def test_ensure_panel_message_prefers_existing_and_does_not_send_new(monkeypatch):
    async def _run():
        sent = {"count": 0}

        class FakeMessage:
            id = 99

        class FakeChannel:
            def permissions_for(self, _member):
                return SimpleNamespace(view_channel=True, send_messages=True, embed_links=True)

            async def fetch_message(self, message_id):
                assert message_id == 99
                return FakeMessage()

            async def send(self, **_kwargs):
                sent["count"] += 1
                return FakeMessage()

        fake_channel = FakeChannel()
        fake_guild = SimpleNamespace(
            id=123,
            get_channel=lambda _cid: fake_channel,
            fetch_channel=lambda _cid: fake_channel,
        )

        async def fake_resolve_bot_member(_guild):
            return object()

        monkeypatch.setattr(events, "_resolve_bot_member", fake_resolve_bot_member)

        class FakeSettingsRepo:
            async def upsert_settings(self, *_args, **_kwargs):
                raise AssertionError("should not upsert when message already exists")

        message = await events._ensure_who_can_jump_panel_message(
            guild=fake_guild,
            settings_repo=FakeSettingsRepo(),
            settings={"who_can_jump_channel_id": 5, "who_can_jump_message_id": 99},
        )

        assert message.id == 99
        assert sent["count"] == 0

    asyncio.run(_run())


def test_refresh_recreates_stale_message(monkeypatch):
    async def _run():
        edits = {"count": 0}

        class FakeNotFound(discord.NotFound):
            def __init__(self):
                super().__init__(SimpleNamespace(status=404, reason="not found"), "gone")

        class FirstMessage:
            async def edit(self, **_kwargs):
                raise FakeNotFound()

        class SecondMessage:
            async def edit(self, **_kwargs):
                edits["count"] += 1

        fake_guild = SimpleNamespace(id=42)
        monkeypatch.setattr(events.bot, "get_guild", lambda _gid: fake_guild)

        class FakeSettingsRepo:
            def __init__(self, _db):
                self.settings = {"who_can_jump_channel_id": 1, "who_can_jump_message_id": 2, "host99k_role_id": 1}

            async def get_settings(self, _gid):
                return dict(self.settings)

            async def upsert_settings(self, _gid, **_fields):
                self.settings.update(_fields)

        monkeypatch.setattr(events, "GuildSettingsRepository", FakeSettingsRepo)
        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(events, "UsersRepository", lambda _pool: object())
        monkeypatch.setattr(events, "JumpsRepository", lambda _pool: object())

        calls = {"count": 0}

        async def fake_ensure(**_kwargs):
            calls["count"] += 1
            return FirstMessage() if calls["count"] == 1 else SecondMessage()

        async def fake_collect(**_kwargs):
            return [{"identity": "Alpha [1]", "status": "Ready Now", "energy": 1000, "drug_cooldown": 0, "booster_cooldown": 0}], None

        monkeypatch.setattr(events, "_ensure_who_can_jump_panel_message", fake_ensure)
        monkeypatch.setattr(events, "_collect_who_can_jump_rows", fake_collect)
        monkeypatch.setattr(events, "_build_who_can_jump_embeds", lambda **_kwargs: [discord.Embed(title="Who Can Jump")])

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

        async def fake_refresh(guild_id):
            called.append(guild_id)
            if guild_id == 1:
                raise RuntimeError("boom")

        monkeypatch.setattr(events, "_refresh_who_can_jump_panel_for_guild", fake_refresh)

        await events.who_can_jump_panel_worker.coro()
        assert called == [1, 2]

    asyncio.run(_run())
