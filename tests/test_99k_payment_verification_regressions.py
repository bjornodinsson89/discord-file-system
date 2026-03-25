import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cogs.events as events


class _FakeResponse:
    async def defer(self, *, ephemeral=False, thinking=False):
        return None


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, embed=None, ephemeral=False, view=None):
        self.messages.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}
        )


class _FakeRepo:
    def __init__(self):
        self.verified = []

    async def get_session(self, _sid):
        return {
            "id": 77,
            "status": "open",
            "guild_id": 1,
            "host_discord_id": 555,
            "created_at": datetime.now(timezone.utc),
            "price_amount": 1,
            "priority_increment": 1,
            "price_item": "xanax",
            "private_channel_id": 222,
        }

    async def is_blacklisted(self, *_args):
        return False

    async def get_signup(self, *_args):
        return {"id": 10}

    async def finalize_priority(self, **_kwargs):
        return True

    async def mark_signup_payment_verified(self, **kwargs):
        self.verified.append(kwargs)
        return True

    async def cancel_expired_unpaid(self):
        return None

    async def list_pending_payment_signups(self, limit=50):
        return [
            {
                "id": 10,
                "session_id": 77,
                "participant_discord_id": 42,
                "host_discord_id": 555,
                "guild_id": 1,
                "price_amount": 1,
                "priority_increment": 1,
                "price_item": "xanax",
                "signup_created_at": datetime.now(timezone.utc),
            }
        ]


class _FakeUsersRepo:
    async def get_user_api_key(self, discord_id):
        if discord_id == 555:
            return {"torn_user_id": 999}
        return {"encrypted_key": "enc", "torn_user_id": 123, "torn_name": "Tester"}


class _FakeTornApi:
    async def verify_xanax_payment(self, *_args, **_kwargs):
        return {"log": "ok"}

    async def verify_dvd_payment(self, *_args, **_kwargs):
        return {"log": "ok"}


class _FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=42)
        self.guild_id = 1
        self.guild = SimpleNamespace(id=1)
        self.client = SimpleNamespace(dispatch=lambda *_args, **_kwargs: None)
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.data = {"custom_id": "x"}


class _FakeWorkerSlot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_manual_verify_succeeds_when_receipt_write_fails(monkeypatch):
    async def _run():
        repo = _FakeRepo()
        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(events, "JumpsRepository", lambda _pool: repo)
        monkeypatch.setattr(events, "UsersRepository", lambda _pool: _FakeUsersRepo())
        monkeypatch.setattr(
            events,
            "get_security_manager",
            lambda: SimpleNamespace(decrypt_api_key=lambda _v: "api"),
        )
        monkeypatch.setattr(events, "get_torn_api", lambda: _FakeTornApi())
        monkeypatch.setattr(
            events, "require_api_key", lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
        )
        monkeypatch.setattr(
            events, "_refresh_99k_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events, "_refresh_or_repost_roster_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events,
            "_grant_private_channel_access",
            lambda *_args, **_kwargs: asyncio.sleep(0, result=True),
        )

        class _FailReceipts:
            def __init__(self, _pool):
                pass

            async def create_and_verify(self, **_kwargs):
                raise RuntimeError("missing table")

        monkeypatch.setattr(events, "PaymentReceiptService", _FailReceipts)

        view = events.Jump99kUserControlsView(77)
        interaction = _FakeInteraction()
        await view.verify_payment.callback(interaction)

        assert repo.verified
        assert interaction.followup.messages
        assert interaction.followup.messages[-1]["content"].startswith("✅ Payment verified")

    asyncio.run(_run())


def test_manual_verify_succeeds_when_private_access_fails(monkeypatch):
    async def _run():
        repo = _FakeRepo()
        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(events, "JumpsRepository", lambda _pool: repo)
        monkeypatch.setattr(events, "UsersRepository", lambda _pool: _FakeUsersRepo())
        monkeypatch.setattr(
            events,
            "get_security_manager",
            lambda: SimpleNamespace(decrypt_api_key=lambda _v: "api"),
        )
        monkeypatch.setattr(events, "get_torn_api", lambda: _FakeTornApi())
        monkeypatch.setattr(
            events, "require_api_key", lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
        )
        monkeypatch.setattr(
            events, "_refresh_99k_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events, "_refresh_or_repost_roster_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events,
            "_grant_private_channel_access",
            lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        )
        monkeypatch.setattr(
            events,
            "PaymentReceiptService",
            lambda _pool: SimpleNamespace(
                create_and_verify=lambda **_kwargs: asyncio.sleep(0, result=1)
            ),
        )

        view = events.Jump99kUserControlsView(77)
        interaction = _FakeInteraction()
        await view.verify_payment.callback(interaction)

        assert repo.verified
        assert interaction.followup.messages[-1]["content"].startswith("✅ Payment verified")

    asyncio.run(_run())


def test_auto_verify_keeps_success_when_receipt_write_fails(monkeypatch):
    async def _run():
        repo = _FakeRepo()

        async def _run_with_lock(_db, _name, fn):
            result = await fn()
            return True, result

        monkeypatch.setattr(
            events, "_worker_db_ready", lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
        )
        monkeypatch.setattr(events, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(events, "get_pool", lambda: object())
        monkeypatch.setattr(events, "JumpsRepository", lambda _pool: repo)
        monkeypatch.setattr(events, "UsersRepository", lambda _pool: _FakeUsersRepo())
        monkeypatch.setattr(
            events,
            "get_security_manager",
            lambda: SimpleNamespace(decrypt_api_key=lambda _v: "api"),
        )
        monkeypatch.setattr(events, "get_torn_api", lambda: _FakeTornApi())
        monkeypatch.setattr(events, "run_with_advisory_lock", _run_with_lock)
        monkeypatch.setattr(
            events, "db_heavy_worker_slot", lambda *_args, **_kwargs: _FakeWorkerSlot()
        )
        monkeypatch.setattr(
            events, "_refresh_99k_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events, "_refresh_or_repost_roster_panel", lambda *_args, **_kwargs: asyncio.sleep(0)
        )
        monkeypatch.setattr(
            events,
            "_grant_private_channel_access",
            lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        )
        events.bot = SimpleNamespace(
            dispatch=lambda *_a, **_k: None, get_guild=lambda _gid: SimpleNamespace(id=1)
        )

        class _FailReceipts:
            def __init__(self, _pool):
                pass

            async def create_and_verify(self, **_kwargs):
                raise RuntimeError("missing table")

        monkeypatch.setattr(events, "PaymentReceiptService", _FailReceipts)

        await events.auto_verify_99k_payments.coro()
        assert repo.verified

    asyncio.run(_run())


def test_fake_schema_mismatch_message_removed_from_manual_99k_path():
    src = Path("cogs/events.py").read_text(encoding="utf-8")
    verify_block = src.split("async def verify_payment", 1)[1].split("class Jump99kSignupView", 1)[
        0
    ]
    assert "database schema mismatch" not in verify_block


def test_payment_receipts_migration_exists_and_has_required_contract():
    src = Path("migrations/2026_03_25_create_payment_receipts.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS public.payment_receipts" in src
    assert "receipt_hash TEXT NOT NULL" in src
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_receipts_receipt_hash" in src
    assert "status IN ('pending', 'verified', 'rejected')" in src
