import asyncio
from types import SimpleNamespace

import cogs.events as events
from utils.command_checks import (
    CommandAccessError,
    has_role_hierarchy_access,
    require_command_access,
)


class _DummyResponse:
    def is_done(self):
        return False


class _DummyInteraction:
    def __init__(self):
        self.response = _DummyResponse()
        self.command = SimpleNamespace(name="testcmd")
        self.guild_id = 123
        self.user = SimpleNamespace(id=456)


def test_on_app_command_error_user_message_is_sanitized(monkeypatch):
    captured = {}

    async def _fake_send_error(interaction, message):
        captured["message"] = message

    monkeypatch.setattr(events, "_send_interaction_error", _fake_send_error)

    interaction = _DummyInteraction()
    asyncio.run(
        events.on_app_command_error(
            interaction, RuntimeError("boom at /tmp/private.py\nTraceback...")
        )
    )

    message = captured["message"]
    assert "Error ID:" in message
    assert "Traceback" not in message
    assert "/tmp/private.py" not in message


def _build_member(*, member_id: int, admin: bool = False, manage_guild: bool = False, roles=()):
    return SimpleNamespace(
        id=member_id,
        guild_permissions=SimpleNamespace(administrator=admin, manage_guild=manage_guild),
        roles=[SimpleNamespace(id=r) for r in roles],
    )


def _build_interaction(*, guild_owner_id: int, member):
    return SimpleNamespace(
        guild=SimpleNamespace(id=999, owner_id=guild_owner_id),
        user=member,
    )


def test_permission_validator_blocks_low_priv_user(monkeypatch):
    check_decorator = require_command_access(
        required_role_setting_keys=("host99k_role_id",), failure_message="nope"
    )

    async def _dummy(_interaction):
        return None

    wrapped = check_decorator(_dummy)
    check = wrapped.__discord_app_commands_checks__[0]

    async def _fake_get_or_create(self, _guild_id):
        return {"host99k_role_id": 999}

    monkeypatch.setattr("utils.command_checks.get_database", lambda: object())
    monkeypatch.setattr(
        "utils.command_checks.GuildSettingsRepository.get_or_create", _fake_get_or_create
    )

    interaction = _build_interaction(
        guild_owner_id=1, member=_build_member(member_id=2, roles=(10,))
    )

    with __import__("pytest").raises(CommandAccessError):
        asyncio.run(check(interaction))


def test_permission_validator_allows_matching_role(monkeypatch):
    check_decorator = require_command_access(
        required_role_setting_keys=("host99k_role_id",), failure_message="nope"
    )

    async def _dummy(_interaction):
        return None

    wrapped = check_decorator(_dummy)
    check = wrapped.__discord_app_commands_checks__[0]

    async def _fake_get_or_create(self, _guild_id):
        return {"host99k_role_id": 999}

    monkeypatch.setattr("utils.command_checks.get_database", lambda: object())
    monkeypatch.setattr(
        "utils.command_checks.GuildSettingsRepository.get_or_create", _fake_get_or_create
    )

    interaction = _build_interaction(
        guild_owner_id=1, member=_build_member(member_id=2, roles=(999,))
    )
    assert asyncio.run(check(interaction)) is True


def test_role_hierarchy_protection_blocks_equal_role():
    class _Role:
        def __gt__(self, _other):
            return False

    guild = SimpleNamespace(owner_id=1)
    actor = SimpleNamespace(id=2, top_role=_Role())
    target = _Role()
    assert has_role_hierarchy_access(guild=guild, actor=actor, target_role=target) is False
