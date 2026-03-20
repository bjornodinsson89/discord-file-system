from pathlib import Path

from utils.embeds import clamp_percent, format_remaining_time, render_text_progress_bar


import asyncio
from types import SimpleNamespace

from cogs.free_raffle import FreeRaffleCog, FreeRaffleModal


class _FakeResponse:
    def __init__(self):
        self.modal = None
        self.messages = []

    def is_done(self) -> bool:
        return bool(self.modal) or bool(self.messages)

    async def send_modal(self, modal):
        self.modal = modal

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _FakeInteraction:
    def __init__(self, user_id: int = 123, guild_id: int = 456):
        self.user = SimpleNamespace(id=user_id, guild_permissions=SimpleNamespace(administrator=False, manage_guild=False), roles=[])
        self.guild_id = guild_id
        self.response = _FakeResponse()


def test_giveaway_start_allows_normal_user_without_api_key_or_special_role(monkeypatch):
    async def _run():
        async def _unexpected_require_api_key(*_args, **_kwargs):
            raise AssertionError("giveaway start should not require an API key")

        monkeypatch.setattr("cogs.free_raffle.require_api_key", _unexpected_require_api_key, raising=False)
        cog = FreeRaffleCog(SimpleNamespace())
        interaction = _FakeInteraction()

        await cog.start.callback(cog, interaction)

        assert isinstance(interaction.response.modal, FreeRaffleModal)
        assert interaction.response.messages == []

    asyncio.run(_run())


def test_giveaway_start_source_has_no_api_key_denial_copy_or_role_gate():
    src = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    assert "register your Torn API key before you can start a giveaway" not in src
    assert 'required_role_setting_keys=("raffle_host_role_id",)' not in src
    assert 'required_role_setting_keys=("admin_role_ids",)' not in src


def test_giveaway_group_rename_and_wording_present():
    src = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    assert 'app_commands.Group(name="giveaway"' in src
    assert '@giveaway.command(name="start"' in src
    assert "Enter Giveaway" in src


def test_progress_bar_helpers():
    assert clamp_percent(-2) == 0.0
    assert clamp_percent(101) == 100.0
    assert render_text_progress_bar(50, width=10).startswith("█████")
    assert format_remaining_time(None) == "Unknown"


def test_token_button_visible_only_when_enabled():
    src = Path("cogs/raffles.py").read_text(encoding="utf-8")
    assert "if self.allow_prize_token_purchase" in src
    assert 'label="🪙 Buy With Prize Tokens"' in src


def test_dispatch_hooks_and_token_fields_wired_in_sources():
    raffles_src = Path("cogs/raffles.py").read_text(encoding="utf-8")
    events_src = Path("cogs/events.py").read_text(encoding="utf-8")
    repo_src = Path("repositories/raffles.py").read_text(encoding="utf-8")

    assert "paid_raffle_purchase_verified" in raffles_src
    assert "jump_99k_purchase_verified" in events_src
    assert "jump_99k_completed" in events_src
    assert "giveaway_joined" in Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    assert "allow_prize_token_purchase" in repo_src
    assert "prize_token_cost_per_ticket" in repo_src
