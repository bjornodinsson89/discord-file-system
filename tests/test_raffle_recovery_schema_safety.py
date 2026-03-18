import asyncio

from cogs.raffles import RafflesCog
from repositories.raffles import RafflesRepository


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecoveryConn:
    def __init__(self, state):
        self.state = state
        self.executed: list[tuple[str, tuple]] = []
        self.inserted_entries: list[dict] = []
        self.new_raffle_id = state.get("new_raffle_id", 200)

    def transaction(self):
        return _Tx()

    async def fetch(self, query, *params):
        if "information_schema.columns" in query:
            table_name = params[0]
            return [{"column_name": column} for column in self.state["columns"][table_name]]
        if "SELECT * FROM raffle_entries" in query:
            raffle_id = params[0]
            return [
                dict(entry)
                for entry in self.state["entries"]
                if entry["raffle_id"] == raffle_id
                and entry["payment_verified"] is True
                and entry.get("recreated_from_entry_id") is None
                and entry.get("refunded_at") is None
                and not entry.get("is_refunded", False)
                and entry.get("status", "verified")
                not in {"refunded", "cancelled", "invalidated", "failed", "pending"}
                and not entry.get("is_cancelled", False)
                and not entry.get("is_invalidated", False)
            ]
        raise AssertionError(query)

    async def fetchrow(self, query, *params):
        if "FROM raffles r" in query and "WHERE r.raffle_id = $1" in query:
            raffle = self.state["raffle"]
            return dict(raffle) if raffle["raffle_id"] == params[0] else None
        if "COUNT(*) AS restored_entry_count" in query:
            entries = await self.fetch("SELECT * FROM raffle_entries", params[0])
            return {
                "restored_entry_count": len(entries),
                "restored_ticket_count": sum(int(entry["num_tickets"]) for entry in entries),
            }
        if "SELECT * FROM raffles WHERE raffle_id = $1 FOR UPDATE" in query:
            raffle = self.state["raffle"]
            return dict(raffle) if raffle["raffle_id"] == params[0] else None
        if "SELECT torn_user_id FROM user_api_keys" in query:
            return {"torn_user_id": 555}
        if "INSERT INTO raffles" in query and "RETURNING raffle_id" in query:
            self.state["insert_raffle_query"] = query
            self.state["insert_raffle_params"] = params
            return {"raffle_id": self.new_raffle_id}
        raise AssertionError(query)

    async def execute(self, query, *params):
        self.executed.append((query, params))
        if query.startswith("INSERT INTO raffle_entries"):
            self.inserted_entries.append(
                {
                    "raffle_id": params[0],
                    "discord_id": params[1],
                    "torn_user_id": params[2],
                    "num_tickets": params[3],
                    "payment_verified": params[5],
                    "recreated_from_entry_id": params[8],
                    "status": params[9],
                }
            )
            return "INSERT 0 1"
        if query.startswith("UPDATE raffles SET"):
            if params[0] == self.state["raffle"]["raffle_id"]:
                self.state["raffle"]["superseded_by_raffle_id"] = params[2]
            return "UPDATE 1"
        raise AssertionError(query)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _repo_with_state(state):
    conn = _RecoveryConn(state)
    repo = RafflesRepository(pool=None)
    repo.acquire = lambda: _Acquire(conn)
    return repo, conn


def test_recreate_cancelled_raffle_is_schema_safe_without_updated_at_and_restores_only_valid_entries():
    state = {
        "new_raffle_id": 321,
        "columns": {
            "raffles": {
                "raffle_id",
                "guild_id",
                "creator_discord_id",
                "creator_torn_id",
                "prize",
                "ticket_payment_type",
                "ticket_price",
                "tickets_available",
                "max_tickets_per_user",
                "end_time",
                "end_trigger",
                "hours_after_sold_out",
                "status",
                "is_free",
                "tickets_sold",
                "is_bundle",
                "bundle_text",
                "admin_comments",
                "allow_prize_token_purchase",
                "prize_token_cost_per_ticket",
                "created_at",
                "recreated_from_raffle_id",
                "superseded_by_raffle_id",
            },
            "raffle_entries": {
                "entry_id",
                "raffle_id",
                "discord_id",
                "torn_user_id",
                "num_tickets",
                "reserved_until",
                "payment_verified",
                "payment_verified_at",
                "created_at",
                "recreated_from_entry_id",
                "status",
                "is_refunded",
                "is_cancelled",
                "is_invalidated",
                "refunded_at",
            },
        },
        "raffle": {
            "raffle_id": 99,
            "guild_id": 1,
            "creator_discord_id": 2,
            "creator_torn_id": None,
            "prize": "Rare prize",
            "ticket_payment_type": "xanax",
            "ticket_price": 3,
            "tickets_available": 50,
            "max_tickets_per_user": 5,
            "end_time": None,
            "end_trigger": "time",
            "hours_after_sold_out": None,
            "status": "cancelled",
            "is_free": False,
            "tickets_sold": 7,
            "is_bundle": False,
            "bundle_text": None,
            "admin_comments": "note",
            "allow_prize_token_purchase": False,
            "prize_token_cost_per_ticket": None,
            "superseded_by_raffle_id": None,
        },
        "entries": [
            {
                "entry_id": 1,
                "raffle_id": 99,
                "discord_id": 10,
                "torn_user_id": 100,
                "num_tickets": 2,
                "payment_verified": True,
                "payment_verified_at": None,
                "is_refunded": False,
                "is_cancelled": False,
                "is_invalidated": False,
                "refunded_at": None,
                "status": "verified",
            },
            {
                "entry_id": 2,
                "raffle_id": 99,
                "discord_id": 11,
                "torn_user_id": 101,
                "num_tickets": 3,
                "payment_verified": True,
                "payment_verified_at": None,
                "is_refunded": True,
                "is_cancelled": False,
                "is_invalidated": False,
                "refunded_at": None,
                "status": "verified",
            },
            {
                "entry_id": 3,
                "raffle_id": 99,
                "discord_id": 12,
                "torn_user_id": 102,
                "num_tickets": 4,
                "payment_verified": False,
                "payment_verified_at": None,
                "is_refunded": False,
                "is_cancelled": False,
                "is_invalidated": False,
                "refunded_at": None,
                "status": "verified",
            },
            {
                "entry_id": 4,
                "raffle_id": 99,
                "discord_id": 13,
                "torn_user_id": 103,
                "num_tickets": 5,
                "payment_verified": True,
                "payment_verified_at": None,
                "is_refunded": False,
                "is_cancelled": True,
                "is_invalidated": False,
                "refunded_at": None,
                "status": "verified",
            },
        ],
    }
    repo, conn = _repo_with_state(state)

    result = asyncio.run(repo.recreate_cancelled_raffle(99))

    assert result["new_raffle_id"] == 321
    assert result["restored_entry_count"] == 1
    assert result["restored_ticket_count"] == 2
    assert state["raffle"]["superseded_by_raffle_id"] == 321
    assert len(conn.inserted_entries) == 1
    assert conn.inserted_entries[0]["discord_id"] == 10
    assert conn.inserted_entries[0]["recreated_from_entry_id"] == 1
    assert conn.inserted_entries[0]["status"] == "verified"
    update_queries = [
        query for query, _params in conn.executed if query.startswith("UPDATE raffles SET")
    ]
    assert any("superseded_by_raffle_id = $3" in query for query in update_queries)
    assert all("updated_at = NOW()" not in query for query in update_queries)


def test_recreate_cancelled_raffle_blocks_duplicate_recovery_once_superseded():
    state = {
        "columns": {
            "raffles": {"raffle_id", "status", "superseded_by_raffle_id"},
            "raffle_entries": set(),
        },
        "raffle": {"raffle_id": 77, "status": "cancelled", "superseded_by_raffle_id": 88},
        "entries": [],
    }
    repo, _conn = _repo_with_state(state)

    try:
        asyncio.run(repo.recreate_cancelled_raffle(77))
    except ValueError as exc:
        assert str(exc) == "This cancelled raffle already has a replacement."
    else:
        raise AssertionError("Expected duplicate recovery to be blocked")


class _FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "ephemeral": ephemeral, **kwargs})


class _FakeInteraction:
    def __init__(self):
        self.followup = _FakeFollowup()
        self.guild = None


def test_recreate_cancelled_raffle_surfaces_clear_admin_error(monkeypatch):
    class _FailingRepo:
        def __init__(self, _pool):
            pass

        async def recreate_cancelled_raffle(self, raffle_id):
            raise RuntimeError('column "updated_at" of relation "raffles" does not exist')

    monkeypatch.setattr("cogs.raffles.RafflesRepository", _FailingRepo)
    monkeypatch.setattr("cogs.raffles.get_pool", lambda: None)

    interaction = _FakeInteraction()
    cog = object.__new__(RafflesCog)

    asyncio.run(RafflesCog.recreate_cancelled_raffle(cog, interaction, 77))

    assert interaction.followup.messages == [
        {
            "content": '❌ Failed to recreate raffle: RuntimeError: column "updated_at" of relation "raffles" does not exist',
            "ephemeral": True,
        }
    ]
