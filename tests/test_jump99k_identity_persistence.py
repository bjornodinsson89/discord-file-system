import asyncio

from repositories.jumps import JumpsRepository


class _FakeConn:
    def __init__(self, columns=None):
        self.columns = columns or []
        self.execute_calls = []
        self.fetch_calls = []
        self.fetchrow_calls = []

    async def execute(self, query, *params):
        self.execute_calls.append((query, params))

    async def fetch(self, query, *params):
        self.fetch_calls.append((query, params))
        if "information_schema.columns" in query:
            return [{"column_name": c} for c in self.columns]
        return []

    async def fetchrow(self, query, *params):
        self.fetchrow_calls.append((query, params))
        return {"id": 1}


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _repo_with_conn(conn):
    repo = JumpsRepository(pool=None)
    repo.acquire = lambda: _Acquire(conn)
    return repo


def test_create_or_restore_signup_persists_torn_name_and_does_not_wipe_on_blank():
    conn = _FakeConn()
    repo = _repo_with_conn(conn)

    asyncio.run(
        repo.create_or_restore_signup(
            session_id=1,
            guild_id=2,
            discord_id=3,
            torn_user_id=444,
            torn_name="  Name  ",
            reserved_until=None,
        )
    )

    query, params = conn.execute_calls[0]
    assert "participant_torn_name" in query
    assert (
        "COALESCE(NULLIF(EXCLUDED.participant_torn_name, ''), jump_99k_signups.participant_torn_name)"
        in query
    )
    assert params[4] == "Name"


def test_mark_signup_payment_verified_backfills_identity_with_schema_drift_safety():
    conn = _FakeConn(
        columns=["payment_source", "participant_torn_name", "participant_torn_user_id"]
    )
    repo = _repo_with_conn(conn)

    ok = asyncio.run(
        repo.mark_signup_payment_verified(
            session_id=10,
            discord_id=20,
            torn_user_id=1234,
            torn_name="  BackfillName  ",
        )
    )

    assert ok is True
    query, params = conn.fetchrow_calls[0]
    assert "participant_torn_user_id=COALESCE($4, participant_torn_user_id)" in query
    assert "participant_torn_name=COALESCE(NULLIF($5, ''), participant_torn_name)" in query
    assert params[3] == 1234
    assert params[4] == "BackfillName"
    assert "payment_source='auto'" in query


def test_mark_signup_payment_verified_handles_optional_columns_absent():
    conn = _FakeConn(columns=[])
    repo = _repo_with_conn(conn)

    ok = asyncio.run(
        repo.mark_signup_payment_verified(
            session_id=10,
            discord_id=20,
            torn_user_id=1234,
            torn_name="Name",
        )
    )

    assert ok is True
    query, params = conn.fetchrow_calls[0]
    assert "participant_torn_user_id" not in query
    assert "participant_torn_name" not in query
    assert "payment_source='auto'" not in query
    assert len(params) == 3
