import pytest

from utils.command_checks import CommandAccessError, validate_interaction_context


class _FakeInteraction:
    def __init__(self, guild=None, guild_id=None, user=None):
        self.guild = guild
        self.guild_id = guild_id
        self.user = user


class _FakeMember:
    def __init__(self, *, bot=False):
        self.bot = bot
        self.roles = []
        self.guild_permissions = object()


def test_validate_interaction_context_rejects_dm_context():
    interaction = _FakeInteraction(guild=None, guild_id=None, user=_FakeMember())

    with pytest.raises(CommandAccessError):
        validate_interaction_context(interaction)  # type: ignore[arg-type]


def test_validate_interaction_context_rejects_bot_user():
    interaction = _FakeInteraction(guild=object(), guild_id=1, user=_FakeMember(bot=True))

    with pytest.raises(CommandAccessError):
        validate_interaction_context(interaction)  # type: ignore[arg-type]
