import asyncio

from repositories.jumps import JumpsRepository
from repositories.raffles import RafflesRepository


class _Tx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.conn._holds_lock:
            self.conn.state["lock"].release()
            self.conn._holds_lock = False


class _AcquireCtx:
    def __init__(self, pool):
        self.pool = pool
        self.conn = None

    async def __aenter__(self):
        self.conn = self.pool._conn_factory(self.pool.state)
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RaffleConn:
    def __init__(self, state):
        self.state = state
        self._holds_lock = False

    def transaction(self):
        return _Tx(self)

    async def fetchrow(self, query, *args):
        if "FROM raffles" in query and "FOR UPDATE" in query:
            await self.state["lock"].acquire()
            self._holds_lock = True
            raffle = self.state["raffle"]
            return {
                "raffle_id": raffle["raffle_id"],
                "status": raffle["status"],
                "tickets_available": raffle["tickets_available"],
                "winner_discord_id": raffle["winner_discord_id"],
                "winner_torn_id": raffle["winner_torn_id"],
                "verified_total": sum(e["num_tickets"] for e in self.state["entries"] if e["payment_verified"]),
            }
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "FROM raffle_entries" in query:
            return [
                {
                    "discord_id": e["discord_id"],
                    "torn_user_id": e["torn_user_id"],
                    "num_tickets": e["num_tickets"],
                }
                for e in self.state["entries"]
                if e["payment_verified"]
            ]
        raise AssertionError(query)

    async def execute(self, query, *args):
        raffle = self.state["raffle"]
        if "SET status = 'drawing'" in query:
            if raffle["status"] == "active":
                raffle["status"] = "drawing"
            return "UPDATE 1"
        if "SET status = 'completed'" in query:
            raffle["status"] = "completed"
            raffle["winner_discord_id"] = args[0]
            raffle["winner_torn_id"] = args[1]
            return "UPDATE 1"
        if "SET status = 'cancelled'" in query:
            raffle["status"] = "cancelled"
            return "UPDATE 1"
        raise AssertionError(query)


class _RafflePool:
    def __init__(self, state):
        self.state = state
        self._conn_factory = _RaffleConn

    def acquire(self):
        return _AcquireCtx(self)


class _JumpConn:
    def __init__(self, state):
        self.state = state
        self._holds_lock = False

    def transaction(self):
        return _Tx(self)

    async def execute(self, query, *args):
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        if "DELETE FROM happy_jump_waitlist" in query:
            session_id, discord_id = args
            self.state["waitlist"] = [w for w in self.state["waitlist"] if not (w["session_id"] == session_id and w["discord_id"] == discord_id)]
            return "DELETE 1"
        if "UPDATE happy_jump_waitlist" in query and "position = position - 1" in query:
            session_id, pos = args
            for row in self.state["waitlist"]:
                if row["session_id"] == session_id and row["position"] > pos:
                    row["position"] -= 1
            return "UPDATE 0"
        if "INSERT INTO happy_jump_signups" in query:
            session_id, discord_id, torn_user_id, _mins = args
            existing = next((s for s in self.state["signups"] if s["session_id"] == session_id and s["discord_id"] == discord_id), None)
            if existing is None:
                self.state["signups"].append(
                    {
                        "session_id": session_id,
                        "discord_id": discord_id,
                        "torn_user_id": torn_user_id,
                        "status": "reserved",
                        "payment_verified": False,
                        "expired": False,
                    }
                )
            else:
                existing.update({"status": "reserved", "payment_verified": False, "expired": False})
            return "INSERT 0 1"
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        if "FROM happy_jump_sessions" in query and "FOR UPDATE" in query:
            await self.state["lock"].acquire()
            self._holds_lock = True
            session_id = args[0]
            row = self.state["sessions"].get(session_id)
            return dict(row) if row else None
        if "FROM happy_jump_waitlist" in query and "FOR UPDATE" in query:
            session_id = args[0]
            rows = [w for w in self.state["waitlist"] if w["session_id"] == session_id]
            rows.sort(key=lambda r: r["position"])
            return dict(rows[0]) if rows else None
        raise AssertionError(query)

    async def fetch(self, query, *args):
        if "DELETE FROM happy_jump_signups" in query and "RETURNING discord_id" in query:
            session_id = args[0]
            expired = [s for s in self.state["signups"] if s["session_id"] == session_id and s["status"] == "reserved" and not s["payment_verified"] and s.get("expired")]
            self.state["signups"] = [s for s in self.state["signups"] if s not in expired]
            return [{"discord_id": s["discord_id"]} for s in expired]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        if "SELECT COUNT(*)" in query and "FROM happy_jump_signups" in query:
            session_id = args[0]
            return sum(
                1
                for s in self.state["signups"]
                if s["session_id"] == session_id and s["status"] in {"reserved", "paid", "completed", "not_completed"}
            )
        raise AssertionError(query)


class _JumpPool:
    def __init__(self, state):
        self.state = state
        self._conn_factory = _JumpConn

    def acquire(self):
        return _AcquireCtx(self)


def test_draw_raffle_winner_atomic_prevents_double_draws():
    state = {
        "lock": asyncio.Lock(),
        "raffle": {
            "raffle_id": 1,
            "status": "active",
            "tickets_available": 1,
            "winner_discord_id": None,
            "winner_torn_id": None,
        },
        "entries": [{"discord_id": 111, "torn_user_id": 222, "num_tickets": 1, "payment_verified": True}],
    }
    repo = RafflesRepository(_RafflePool(state))

    async def _run():
        first, second = await asyncio.gather(
            repo.draw_raffle_winner_atomic(1),
            repo.draw_raffle_winner_atomic(1),
        )
        return first, second

    first, second = asyncio.run(_run())

    states = {first["state"], second["state"]}
    assert "drawn" in states
    assert "already_drawn" in states
    assert state["raffle"]["status"] == "completed"
    assert state["raffle"]["winner_discord_id"] == 111


def test_expire_and_promote_waitlist_atomic_prevents_duplicate_promotion():
    state = {
        "lock": asyncio.Lock(),
        "sessions": {1: {"id": 1, "max_spots": 1, "status": "open"}},
        "signups": [
            {"session_id": 1, "discord_id": 10, "status": "reserved", "payment_verified": False, "expired": True},
        ],
        "waitlist": [{"session_id": 1, "discord_id": 20, "torn_user_id": 200, "position": 1}],
    }
    repo = JumpsRepository(_JumpPool(state))

    async def _run():
        return await asyncio.gather(
            repo.expire_and_promote_waitlist(session_id=1, reservation_minutes=5),
            repo.expire_and_promote_waitlist(session_id=1, reservation_minutes=5),
        )

    results = asyncio.run(_run())

    promoted = [r for r in results if r["promoted_discord_id"] is not None]
    assert len(promoted) == 1
    assert promoted[0]["promoted_discord_id"] == 20
    promoted_signups = [s for s in state["signups"] if s["discord_id"] == 20]
    assert len(promoted_signups) == 1
    assert state["waitlist"] == []
