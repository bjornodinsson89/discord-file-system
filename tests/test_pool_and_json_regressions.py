import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace
from uuid import uuid4

import cogs.events as events
import cogs.pools as pools
from repositories.engagement import EngagementRepository
from services.payment_receipts import PaymentReceiptService


class _EnumSample(Enum):
    A = "alpha"


def test_payment_receipt_compact_json_handles_datetime_payloads():
    payload = {
        "dt": datetime(2026, 4, 19, 1, 2, 3, tzinfo=timezone.utc),
        "d": date(2026, 4, 19),
        "t": time(1, 2, 3),
        "n": Decimal("10.50"),
        "u": uuid4(),
        "set": {1, 2},
        "tuple": ("x", "y"),
        "e": _EnumSample.A,
        "bytes": b"hello",
        "nested": {"x": datetime(2026, 4, 19, tzinfo=timezone.utc)},
    }
    raw = PaymentReceiptService._compact_json(payload)
    parsed = json.loads(raw)
    assert parsed["dt"].startswith("2026-04-19T01:02:03")
    assert parsed["d"] == "2026-04-19"
    assert parsed["t"].startswith("01:02:03")
    assert parsed["n"] == "10.50"
    assert isinstance(parsed["u"], str)
    assert sorted(parsed["set"]) == [1, 2]
    assert parsed["tuple"] == ["x", "y"]
    assert parsed["e"] == "alpha"
    assert parsed["bytes"] == "hello"
    assert parsed["nested"]["x"].startswith("2026-04-19T00:00:00")


def test_engagement_insert_event_ledger_handles_datetime_payloads():
    async def _run():
        captured = {}

        class _Conn:
            async def fetchrow(self, _sql, *args):
                captured["payload_json"] = args[-1]
                return {"id": 1}

        class _Ctx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        repo = EngagementRepository(object())
        repo.acquire = lambda: _Ctx()
        applied = await repo.insert_event_ledger(
            guild_id=1,
            user_id=2,
            event_name="jump_99k_purchase_verified",
            source_type="jump_99k",
            source_id="3",
            dedupe_key="k",
            xp_delta=10,
            payload={"when": datetime(2026, 4, 19, tzinfo=timezone.utc)},
        )
        assert applied is True
        parsed = json.loads(captured["payload_json"])
        assert parsed["when"].startswith("2026-04-19T00:00:00")

    asyncio.run(_run())


class _FakeResponse:
    async def send_message(self, *_args, **_kwargs):
        return None

    async def defer(self, *_args, **_kwargs):
        return None


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeRepo:
    def __init__(self):
        now = datetime.now(timezone.utc)
        self.pending = {
            "id": 7,
            "quantity": 2,
            "total_cost_xanax": 10,
            "buyer_torn_user_id": 111,
            "created_at": now,
            "reserved_until": now + timedelta(minutes=3),
        }
        self.added = []
        self.marked = []

    async def get_pool(self, _pool_id):
        return {
            "status": "active",
            "created_by_discord_id": 999,
            "tickets_total": 100,
            "max_per_user": 0,
            "unlimited_tickets": False,
            "ticket_price_xanax": 5,
        }

    async def get_pending_purchase(self, _pool_id, _discord_id):
        return dict(self.pending)

    async def get_total_tickets(self, _pool_id):
        return 0

    async def get_user_tickets(self, _pool_id, _discord_id):
        return 0

    async def add_entry(self, pool_id, user_id, quantity):
        self.added.append((pool_id, user_id, quantity))

    async def mark_pending_purchase_verified(self, pending_id):
        self.marked.append(pending_id)


class _FakeUsersRepo:
    async def get_user_api_key(self, discord_id):
        if int(discord_id) == 999:
            return {"encrypted_key": "enc_creator", "torn_user_id": 222, "torn_name": "Creator"}
        return {"encrypted_key": "enc_buyer", "torn_user_id": 111, "torn_name": "Buyer"}


def test_pool_verify_succeeds_when_receipt_write_fails_and_sets_verifier_metadata(monkeypatch):
    async def _run():
        fake_repo = _FakeRepo()
        receipt_calls = []

        monkeypatch.setattr(pools, "PoolsRepository", lambda _pool: fake_repo)
        monkeypatch.setattr(pools, "get_pool", lambda: object())
        monkeypatch.setattr(pools, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(pools, "UsersRepository", lambda _pool: _FakeUsersRepo())
        monkeypatch.setattr(
            pools,
            "get_security_manager",
            lambda: SimpleNamespace(decrypt_api_key=lambda encrypted: f"plain:{encrypted}"),
        )
        monkeypatch.setattr(
            pools,
            "get_torn_api",
            lambda: SimpleNamespace(
                get_item_send_receive_logs=lambda *_a, **_k: asyncio.sleep(
                    0, result=[{"timestamp": int(datetime.now(timezone.utc).timestamp())}]
                )
            ),
        )
        monkeypatch.setattr(
            pools,
            "RafflePaymentService",
            lambda _db: SimpleNamespace(
                _find_matching_payment=lambda **_kwargs: {
                    "timestamp": int(datetime.now(timezone.utc).timestamp())
                },
                _summarize_payment_match_stages=lambda **_kwargs: (1, 1, 1),
            ),
        )
        monkeypatch.setattr(
            pools, "_refresh_pool_panel_message", lambda *_a, **_k: asyncio.sleep(0)
        )

        class _FailReceipts:
            def __init__(self, _pool):
                pass

            async def create_and_verify(self, **kwargs):
                receipt_calls.append(kwargs)
                raise RuntimeError("schema drift")

        monkeypatch.setattr(pools, "PaymentReceiptService", _FailReceipts)

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=_FakeResponse(),
            followup=_FakeFollowup(),
        )
        view = pools.PoolVerifyPaymentView(bot=SimpleNamespace(), pool_id=5, owner_discord_id=42)
        await view.verify_payment.callback(interaction)

        assert fake_repo.added == [(5, 42, 2)]
        assert fake_repo.marked == [7]
        assert interaction.followup.messages[-1]["content"].startswith("✅ Purchase verified")
        assert "receipt warning" in interaction.followup.messages[-1]["content"]
        assert receipt_calls
        assert receipt_calls[0]["verifier_torn_id"] == 111

    asyncio.run(_run())


def test_payment_receipts_defensive_migration_is_idempotent_contract():
    src = open(
        "migrations/2026_04_19_backfill_payment_receipts_schema.sql", encoding="utf-8"
    ).read()
    assert "ALTER TABLE IF EXISTS public.payment_receipts" in src
    assert "ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in src
    assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in src
    assert "ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ NULL" in src
    assert "ADD COLUMN IF NOT EXISTS verified_by_discord_id BIGINT NULL" in src
    assert "ADD COLUMN IF NOT EXISTS receipt_meta JSONB NOT NULL DEFAULT '{}'::jsonb" in src


def test_pool_verify_surfaces_verification_errors_before_payment_not_found(monkeypatch):
    async def _run():
        fake_repo = _FakeRepo()

        monkeypatch.setattr(pools, "PoolsRepository", lambda _pool: fake_repo)
        monkeypatch.setattr(pools, "get_pool", lambda: object())
        monkeypatch.setattr(pools, "get_database", lambda: SimpleNamespace(pool=object()))
        monkeypatch.setattr(pools, "UsersRepository", lambda _pool: _FakeUsersRepo())
        monkeypatch.setattr(
            pools,
            "get_security_manager",
            lambda: SimpleNamespace(decrypt_api_key=lambda encrypted: f"plain:{encrypted}"),
        )

        class _Torn:
            async def get_item_send_receive_logs(self, *_args, audit_context=None, **_kwargs):
                if audit_context == "pool_payment_verify_creator_logs":
                    raise pools.TornAPIPermissionError("creator missing cat85")
                raise pools.TornAPIError("buyer unavailable")

        monkeypatch.setattr(pools, "get_torn_api", lambda: _Torn())
        monkeypatch.setattr(
            pools,
            "RafflePaymentService",
            lambda _db: SimpleNamespace(
                _find_matching_payment=lambda **_kwargs: None,
                _summarize_payment_match_stages=lambda **_kwargs: (0, 0, 0),
            ),
        )

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=_FakeResponse(),
            followup=_FakeFollowup(),
        )
        view = pools.PoolVerifyPaymentView(bot=SimpleNamespace(), pool_id=5, owner_discord_id=42)
        await view.verify_payment.callback(interaction)

        assert fake_repo.added == []
        assert interaction.followup.messages
        content = interaction.followup.messages[-1]["content"] or ""
        assert "Payment not found" not in content
        assert "Torn verification" in content or "missing item-log permissions" in content

    asyncio.run(_run())


def test_cached_readiness_upsert_failure_returns_none(monkeypatch):
    async def _run():
        class _UsersRepo:
            async def get_user_api_key(self, _discord_id):
                return {"encrypted_key": "enc"}

        class _Repo:
            async def upsert_readiness_snapshot(self, **_kwargs):
                raise RuntimeError("db down")

        cache_key = (77, 42)
        original_cache = dict(events._READINESS_FETCH_CACHE)
        events._READINESS_FETCH_CACHE.clear()
        events._READINESS_FETCH_CACHE[cache_key] = (
            datetime.now(timezone.utc),
            {
                "session_id": 77,
                "guild_id": 1,
                "discord_id": 42,
                "energy": 1000,
                "energy_max": 1000,
                "drug_cooldown": 0,
                "booster_cooldown": 0,
                "status_text": "ready",
            },
        )
        try:
            payload = await events._fetch_and_upsert_user_readiness_snapshot(
                repo=_Repo(),
                users_repo=_UsersRepo(),
                session_id=77,
                guild_id=1,
                discord_id=42,
            )
            assert payload is None
        finally:
            events._READINESS_FETCH_CACHE.clear()
            events._READINESS_FETCH_CACHE.update(original_cache)

    asyncio.run(_run())
