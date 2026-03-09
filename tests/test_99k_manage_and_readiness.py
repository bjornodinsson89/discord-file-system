import asyncio

from cogs.events import (
    _all_active_non_finished_ready,
    _readiness_poll_seconds,
    _list_removable_signups,
    _apply_energy_poll,
)
from repositories.jumps import JumpsRepository


class _FakeRepoForRemoveList:
    def __init__(self, progress_rows, roster_rows):
        self._progress_rows = progress_rows
        self._roster_rows = roster_rows

    async def get_jump_progress(self, _session_id: int):
        return {"signups": self._progress_rows}

    async def list_roster_signups_with_readiness(self, _session_id: int):
        return self._roster_rows


def test_manage_remove_flow_lists_only_valid_removable_users():
    repo = _FakeRepoForRemoveList(
        progress_rows=[
            {"discord_id": 2, "state": "waiting"},
            {"discord_id": 3, "state": "in_progress"},
        ],
        roster_rows=[
            {"discord_id": 1, "status": "paid", "is_priority": False},
            {"discord_id": 2, "status": "paid", "is_priority": True},
            {"discord_id": 3, "status": "paid", "is_priority": False},
            {"discord_id": 4, "status": "cancelled", "is_priority": False},
        ],
    )
    session = {"id": 99, "host_discord_id": 1}
    rows = asyncio.run(_list_removable_signups(repo=repo, session=session))
    assert [int(r["discord_id"]) for r in rows] == [2]


def test_readiness_hot_poll_logic():
    readiness_rows = [
        {"discord_id": 11, "status_text": "Ready"},
        {"discord_id": 12, "status_text": "ready"},
    ]
    assert _all_active_non_finished_ready(
        active_non_finished_discord_ids=[11, 12], readiness_rows=readiness_rows
    )
    assert (
        _readiness_poll_seconds(
            all_active_non_finished_ready=True, active_seconds=30, hot_seconds=10
        )
        == 10
    )
    assert (
        _readiness_poll_seconds(
            all_active_non_finished_ready=False, active_seconds=30, hot_seconds=10
        )
        == 30
    )
    assert not _all_active_non_finished_ready(
        active_non_finished_discord_ids=[], readiness_rows=readiness_rows
    )


class _FakeConn:
    def __init__(
        self,
        *,
        host_discord_id: int,
        signup_exists: bool = True,
        signup_status: str = "paid",
        jump_state: str = "waiting",
    ):
        self.host_discord_id = host_discord_id
        self.signup_exists = signup_exists
        self.signup_status = signup_status
        self.jump_state = jump_state
        self.updated = False

    async def fetchrow(self, query, *args):
        if "FROM jump_99k_sessions" in query:
            return {"id": args[0], "status": "open", "host_discord_id": self.host_discord_id}
        if "FROM jump_99k_signups" in query and "FOR UPDATE" in query:
            if not self.signup_exists:
                return None
            return {"id": 88, "status": self.signup_status}
        return None

    async def fetchval(self, query, *args):
        if "SELECT jump_state" in query:
            return self.jump_state
        return None

    async def execute(self, query, *args):
        if "UPDATE jump_99k_signups" in query:
            self.updated = True

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _build_repo(conn):
    repo = JumpsRepository(pool=None)
    repo.acquire = lambda: _FakeAcquire(conn)
    return repo


def test_manual_remove_cannot_remove_host_or_in_progress_and_can_succeed():
    conn_host = _FakeConn(host_discord_id=5)
    repo_host = _build_repo(conn_host)
    ok, msg = asyncio.run(
        repo_host.manual_remove_signup(session_id=1, removed_discord_id=5, removed_by_discord_id=99)
    )
    assert not ok
    assert "Host cannot be removed" in msg

    conn_in_progress = _FakeConn(host_discord_id=5, jump_state="in_progress")
    repo_progress = _build_repo(conn_in_progress)
    ok, msg = asyncio.run(
        repo_progress.manual_remove_signup(
            session_id=1, removed_discord_id=7, removed_by_discord_id=99
        )
    )
    assert not ok
    assert "currently in progress" in msg

    conn_ok = _FakeConn(host_discord_id=5, jump_state="waiting")
    repo_ok = _build_repo(conn_ok)
    ok, msg = asyncio.run(
        repo_ok.manual_remove_signup(session_id=1, removed_discord_id=7, removed_by_discord_id=99)
    )
    assert ok
    assert "Removed" in msg
    assert conn_ok.updated is True


def test_public_roster_panel_has_only_three_buttons():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    assert 'label="Refresh roster"' in events_py
    assert 'label="View roster"' in events_py
    assert 'label="Host Controls"' in events_py
    assert 'label="Manage"' not in events_py


def test_host_controls_has_required_buttons():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    assert 'label="Start Jump"' in events_py
    assert 'label="Manage Jumpers"' in events_py
    assert 'label="Pause Jump"' in events_py
    assert 'label="Delete This Jump"' in events_py
    assert 'label="Reset Progress"' not in events_py


def test_energy_rule_requires_seen_nonzero_then_four_lows():
    saw, lows, done = _apply_energy_poll(saw_nonzero_energy=False, consecutive_low_energy_polls=0, energy=5)
    assert (saw, lows, done) == (True, 0, False)
    saw, lows, done = _apply_energy_poll(saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=0)
    assert done is False
    saw, lows, done = _apply_energy_poll(saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=0)
    assert done is False
    saw, lows, done = _apply_energy_poll(saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=0)
    assert done is False
    saw, lows, done = _apply_energy_poll(saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=0)
    assert done is True
