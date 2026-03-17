import asyncio

from cogs.events import (
    _all_active_non_finished_ready,
    _readiness_poll_seconds,
    _list_removable_signups,
    _apply_energy_poll,
    _build_jump_transition_notification,
    _removable_signup_option_label,
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


def test_public_roster_panel_has_only_refresh_and_host_controls_buttons():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    assert 'label="Refresh roster"' in events_py
    assert 'label="View roster"' not in events_py
    assert 'label="Host Controls"' in events_py
    assert 'label="Manage"' not in events_py


def test_host_controls_has_required_buttons():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    assert 'label="Start Jump"' in events_py
    assert 'label="Manage Jumpers"' in events_py
    assert 'label="Pause Jump"' in events_py
    assert 'label="Delete This Jump"' in events_py
    assert 'label="Reset Progress"' not in events_py


def test_prestart_and_started_cadence_values_are_15_and_3_seconds():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    assert "@tasks.loop(seconds=3)" in events_py
    assert "next_seconds = 3 if jump_started else 15" in events_py


def test_prestart_roster_worker_skips_started_sessions():
    events_py = __import__("pathlib").Path("cogs/events.py").read_text(encoding="utf-8")
    roster_block = events_py.split("async def roster_panel_refresh_worker", 1)[1].split(
        "@roster_panel_refresh_worker.before_loop", 1
    )[0]
    assert "jump_started = await _session_jump_started(repo, session_id)" in roster_block
    assert "if jump_started:" in roster_block
    assert "await _refresh_or_repost_roster_panel(bot, session_id)" in roster_block


def test_energy_rule_requires_seen_nonzero_then_four_lows():
    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=False, consecutive_low_energy_polls=0, energy=5
    )
    assert (saw, lows, done) == (False, 0, False)

    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=False, consecutive_low_energy_polls=0, energy=25
    )
    assert (saw, lows, done) == (True, 0, False)

    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=9
    )
    assert (saw, lows, done) == (True, 1, False)
    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=8
    )
    assert (saw, lows, done) == (True, 2, False)

    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=12
    )
    assert (saw, lows, done) == (True, 0, False)

    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=9
    )
    assert (saw, lows, done) == (True, 1, False)
    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=8
    )
    assert (saw, lows, done) == (True, 2, False)
    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=4
    )
    assert (saw, lows, done) == (True, 3, False)
    saw, lows, done = _apply_energy_poll(
        saw_nonzero_energy=saw, consecutive_low_energy_polls=lows, energy=0
    )
    assert (saw, lows, done) == (True, 4, True)


class _ManualAddFakeRepo:
    def __init__(self, *, add_result=(True, "ok"), add_raises: Exception | None = None):
        self.add_result = add_result
        self.add_raises = add_raises
        self.manual_add_called = False

    async def get_session(self, _session_id: int):
        return {"id": 99, "status": "open", "guild_id": 1}

    async def manual_add_as_verified_signup(self, **_kwargs):
        self.manual_add_called = True
        if self.add_raises is not None:
            raise self.add_raises
        return self.add_result


class _ManualAddFakeUsersRepo:
    async def get_user_api_key(self, _discord_id: int):
        return {"encrypted_key": "k", "torn_user_id": 1, "torn_name": "name"}


class _ManualAddFakeSelectedUser:
    id = 123
    mention = "<@123>"

    async def send(self, _msg: str):
        return None


class _ManualAddFakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class _ManualAddFakeInteractionUser:
    id = 555


class _ManualAddFakeInteraction:
    def __init__(self):
        self.user = _ManualAddFakeInteractionUser()
        self.guild_id = 1
        self.guild = object()
        self.client = None
        self.followup = _ManualAddFakeFollowup()


def test_manual_add_success_survives_post_add_side_effect_failure():
    from cogs import events

    captured = {}

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_safe_edit_original(_interaction, *, content=None, embed=None, view=None):
        captured["content"] = content
        captured["view"] = view

    async def _fake_can_use_manual_add_controls(_interaction, _session):
        return True

    async def _fail_grant_access(_guild, _session, _discord_id):
        raise RuntimeError("boom")

    _missing = object()
    original_can_use = getattr(events, "_can_use_manual_add_controls", _missing)
    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "_safe_edit_original": events._safe_edit_original,
        "_grant_private_channel_access": events._grant_private_channel_access,
        "JumpsRepository": events.JumpsRepository,
        "UsersRepository": events.UsersRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events._safe_edit_original = _fake_safe_edit_original
        events._can_use_manual_add_controls = _fake_can_use_manual_add_controls
        events._grant_private_channel_access = _fail_grant_access
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: _ManualAddFakeRepo()
        events.UsersRepository = lambda _pool: _ManualAddFakeUsersRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            view.user_select = type("_Select", (), {"values": [_ManualAddFakeSelectedUser()]})()
            interaction = _ManualAddFakeInteraction()
            await view._on_select_user(interaction)
            return interaction

        _ = asyncio.run(_run_case())

        assert captured["content"].startswith("✅ Added <@123> to the jump.")
        assert "follow-up updates could not be completed" in captured["content"]
    finally:
        for name, value in originals.items():
            setattr(events, name, value)
        if original_can_use is _missing:
            delattr(events, "_can_use_manual_add_controls")
        else:
            events._can_use_manual_add_controls = original_can_use


def test_manual_add_preserves_business_logic_failure_message():
    from cogs import events

    captured = {}

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_safe_edit_original(_interaction, *, content=None, embed=None, view=None):
        captured["content"] = content

    async def _fake_can_use_manual_add_controls(_interaction, _session):
        return True

    _missing = object()
    original_can_use = getattr(events, "_can_use_manual_add_controls", _missing)
    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "_safe_edit_original": events._safe_edit_original,
        "JumpsRepository": events.JumpsRepository,
        "UsersRepository": events.UsersRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events._safe_edit_original = _fake_safe_edit_original
        events._can_use_manual_add_controls = _fake_can_use_manual_add_controls
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: _ManualAddFakeRepo(
            add_result=(False, "Session full.")
        )
        events.UsersRepository = lambda _pool: _ManualAddFakeUsersRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            view.user_select = type("_Select", (), {"values": [_ManualAddFakeSelectedUser()]})()
            interaction = _ManualAddFakeInteraction()
            await view._on_select_user(interaction)
            return interaction

        interaction = asyncio.run(_run_case())

        assert interaction.followup.messages[0][0] == "Session full."
        assert captured == {}
    finally:
        for name, value in originals.items():
            setattr(events, name, value)
        if original_can_use is _missing:
            delattr(events, "_can_use_manual_add_controls")
        else:
            events._can_use_manual_add_controls = original_can_use


def test_manual_add_generic_failure_only_for_unexpected_critical_error():
    from cogs import events

    captured = {}

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_safe_edit_original(_interaction, *, content=None, embed=None, view=None):
        captured["content"] = content

    async def _fake_can_use_manual_add_controls(_interaction, _session):
        return True

    _missing = object()
    original_can_use = getattr(events, "_can_use_manual_add_controls", _missing)
    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "_safe_edit_original": events._safe_edit_original,
        "JumpsRepository": events.JumpsRepository,
        "UsersRepository": events.UsersRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events._safe_edit_original = _fake_safe_edit_original
        events._can_use_manual_add_controls = _fake_can_use_manual_add_controls
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: _ManualAddFakeRepo(
            add_raises=RuntimeError("db boom")
        )
        events.UsersRepository = lambda _pool: _ManualAddFakeUsersRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            view.user_select = type("_Select", (), {"values": [_ManualAddFakeSelectedUser()]})()
            interaction = _ManualAddFakeInteraction()
            await view._on_select_user(interaction)

        asyncio.run(_run_case())

        assert captured["content"] == "Sorry—could not add that user. Please try again."
    finally:
        for name, value in originals.items():
            setattr(events, name, value)
        if original_can_use is _missing:
            delattr(events, "_can_use_manual_add_controls")
        else:
            events._can_use_manual_add_controls = original_can_use


def test_manual_add_select_uses_manage_permissions_and_reaches_repo_call():
    from cogs import events

    fake_repo = _ManualAddFakeRepo()
    recorded = {}

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_safe_edit_original(_interaction, *, content=None, embed=None, view=None):
        recorded["content"] = content

    async def _fake_can_manage(_interaction, _session):
        return True

    async def _noop_async(*_args, **_kwargs):
        return None

    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "_safe_edit_original": events._safe_edit_original,
        "can_manage_99k_session": events.can_manage_99k_session,
        "_grant_private_channel_access": events._grant_private_channel_access,
        "JumpsRepository": events.JumpsRepository,
        "UsersRepository": events.UsersRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events._safe_edit_original = _fake_safe_edit_original
        events.can_manage_99k_session = _fake_can_manage
        events._grant_private_channel_access = _noop_async
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: fake_repo
        events.UsersRepository = lambda _pool: _ManualAddFakeUsersRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            view.user_select = type("_Select", (), {"values": [_ManualAddFakeSelectedUser()]})()
            await view._on_select_user(_ManualAddFakeInteraction())

        asyncio.run(_run_case())

        assert fake_repo.manual_add_called is True
        assert recorded["content"].startswith("✅ Added <@123> to the jump.")
    finally:
        for name, value in originals.items():
            setattr(events, name, value)


def test_manual_add_select_denies_unauthorized_user_without_nameerror():
    from cogs import events

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_can_manage(_interaction, _session):
        return False

    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "can_manage_99k_session": events.can_manage_99k_session,
        "JumpsRepository": events.JumpsRepository,
        "UsersRepository": events.UsersRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events.can_manage_99k_session = _fake_can_manage
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: _ManualAddFakeRepo()
        events.UsersRepository = lambda _pool: _ManualAddFakeUsersRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            view.user_select = type("_Select", (), {"values": [_ManualAddFakeSelectedUser()]})()
            interaction = _ManualAddFakeInteraction()
            await view._on_select_user(interaction)
            return interaction

        interaction = asyncio.run(_run_case())
        assert interaction.followup.messages[0][0] == "You do not have permission."
    finally:
        for name, value in originals.items():
            setattr(events, name, value)


def test_manual_add_cancel_uses_manage_permissions_without_nameerror():
    from cogs import events

    captured = {}

    async def _fake_safe_defer_ephemeral(_interaction):
        return None

    async def _fake_safe_edit_original(_interaction, *, content=None, embed=None, view=None):
        captured["content"] = content

    async def _fake_can_manage(_interaction, _session):
        return True

    originals = {
        "_safe_defer_ephemeral": events._safe_defer_ephemeral,
        "_safe_edit_original": events._safe_edit_original,
        "can_manage_99k_session": events.can_manage_99k_session,
        "JumpsRepository": events.JumpsRepository,
        "get_pool": events.get_pool,
    }
    try:
        events._safe_defer_ephemeral = _fake_safe_defer_ephemeral
        events._safe_edit_original = _fake_safe_edit_original
        events.can_manage_99k_session = _fake_can_manage
        events.get_pool = lambda: None
        events.JumpsRepository = lambda _pool: _ManualAddFakeRepo()

        async def _run_case():
            view = events.Jump99kManualAddPickerView(session_id=99)
            await view._on_cancel(_ManualAddFakeInteraction())

        asyncio.run(_run_case())
        assert captured["content"] == "Cancelled."
    finally:
        for name, value in originals.items():
            setattr(events, name, value)


class _TransitionFakeUsersRepo:
    def __init__(self, rows_by_discord_id: dict[int, dict] | None = None):
        self.rows_by_discord_id = rows_by_discord_id or {}

    async def get_user_api_key(self, discord_id: int):
        return self.rows_by_discord_id.get(int(discord_id))


class _FakeMember:
    def __init__(self, display_name: str):
        self.display_name = display_name


class _FakeGuild:
    def __init__(self, member_map: dict[int, _FakeMember] | None = None):
        self.member_map = member_map or {}

    def get_member(self, discord_id: int):
        return self.member_map.get(int(discord_id))

    async def fetch_member(self, discord_id: int):
        member = self.member_map.get(int(discord_id))
        if member is None:
            raise RuntimeError("not found")
        return member


def test_jump_transition_notification_uses_required_torn_identity_and_ping():
    session = {"host_discord_id": 10}
    roster_rows = [
        {
            "discord_id": 10,
            "participant_torn_user_id": 3666214,
            "participant_torn_name": "BjornOdinnsson89",
        },
        {"discord_id": 11, "participant_torn_user_id": 1234567, "participant_torn_name": "Papanad"},
    ]
    users_repo = _TransitionFakeUsersRepo()

    message = asyncio.run(
        _build_jump_transition_notification(
            users_repo=users_repo,
            session=session,
            roster_rows=roster_rows,
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild(),
        )
    )

    assert "BjornOdinnsson89[3666214] is finished" in message
    assert "<@11>" in message
    assert "Papanad[1234567]" in message


def test_jump_transition_notification_recovers_non_host_identity_from_api_key():
    session = {"host_discord_id": 10}
    roster_rows = [{"discord_id": 10, "participant_torn_name": "HostOnlyName"}, {"discord_id": 11}]
    users_repo = _TransitionFakeUsersRepo(
        rows_by_discord_id={11: {"torn_name": "Recovered", "torn_user_id": 5678}}
    )

    message = asyncio.run(
        _build_jump_transition_notification(
            users_repo=users_repo,
            session=session,
            roster_rows=roster_rows,
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild(),
        )
    )

    assert "Recovered[5678]" in message
    assert "User 11" not in message


def test_jump_transition_notification_uses_display_name_with_torn_id_when_name_missing():
    session = {"host_discord_id": 10}
    roster_rows = [
        {"discord_id": 10, "participant_torn_user_id": 999},
        {"discord_id": 11, "participant_torn_user_id": 7777},
    ]

    message = asyncio.run(
        _build_jump_transition_notification(
            users_repo=_TransitionFakeUsersRepo(),
            session=session,
            roster_rows=roster_rows,
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild({10: _FakeMember("PrevName"), 11: _FakeMember("NextDisplay")}),
        )
    )

    assert "PrevName[999] is finished" in message
    assert "NextDisplay[7777]" in message


def test_jump_transition_notification_uses_name_only_when_only_torn_name_exists():
    message = asyncio.run(
        _build_jump_transition_notification(
            users_repo=_TransitionFakeUsersRepo(),
            session={"host_discord_id": 10},
            roster_rows=[
                {"discord_id": 10, "participant_torn_name": "OnlyHostName"},
                {"discord_id": 11, "participant_torn_name": "NextOnlyName"},
            ],
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild(),
        )
    )
    assert "OnlyHostName is finished" in message
    assert "NextOnlyName" in message


def test_jump_transition_notification_falls_back_to_display_name_or_mention_not_user_id():
    message_display = asyncio.run(
        _build_jump_transition_notification(
            users_repo=_TransitionFakeUsersRepo(),
            session={"host_discord_id": 10},
            roster_rows=[{"discord_id": 10}, {"discord_id": 11}],
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild({10: _FakeMember("PrevDisplay"), 11: _FakeMember("NextDisplay")}),
        )
    )
    assert "PrevDisplay is finished" in message_display
    assert "NextDisplay" in message_display
    assert "User 10" not in message_display
    assert "User 11" not in message_display

    message_mention = asyncio.run(
        _build_jump_transition_notification(
            users_repo=_TransitionFakeUsersRepo(),
            session={"host_discord_id": 10},
            roster_rows=[{"discord_id": 10}, {"discord_id": 11}],
            previous_discord_id=10,
            next_discord_id=11,
            guild=_FakeGuild(),
        )
    )
    assert "<@10> is finished" in message_mention
    assert "<@11>" in message_mention


def test_removable_signup_option_label_never_uses_raw_discord_id():
    label, _ = _removable_signup_option_label(
        {
            "discord_id": 240380367066890240,
            "participant_torn_user_id": 3747168,
            "display_name": "Display",
        }
    )
    assert label == "Display[3747168]"

    label_name, _ = _removable_signup_option_label(
        {"discord_id": 240380367066890240, "participant_torn_name": "TornName"}
    )
    assert label_name.startswith("TornName")
    assert "User 240380367066890240" not in label_name
