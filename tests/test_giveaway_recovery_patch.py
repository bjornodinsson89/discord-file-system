import asyncio

from pathlib import Path

from cogs.free_raffle import GIVEAWAY_ENTRY_MODE_CHOICES
from views.free_raffle_views import EnterRaffleView, HostControlsView


async def _noop(*_args, **_kwargs):
    return None


def test_giveaway_entry_modes_include_required_modes():
    assert set(GIVEAWAY_ENTRY_MODE_CHOICES) == {
        "button",
        "auto",
        "button_weighted",
        "auto_weighted",
    }
    assert GIVEAWAY_ENTRY_MODE_CHOICES["button"]["button_join_enabled"] is True
    assert GIVEAWAY_ENTRY_MODE_CHOICES["auto"]["auto_entry_enabled"] is True
    assert GIVEAWAY_ENTRY_MODE_CHOICES["button_weighted"]["weighted_enabled"] is True
    assert GIVEAWAY_ENTRY_MODE_CHOICES["auto_weighted"]["weighted_enabled"] is True


def test_button_join_mode_shows_join_button_and_auto_only_hides_it():
    async def _build():
        shown = EnterRaffleView(raffle_id=1, on_enter=_noop, show_join_button=True)
        hidden = EnterRaffleView(raffle_id=1, on_enter=_noop, show_join_button=False)
        assert any(getattr(child, "custom_id", "") == "fr_enter:1" for child in shown.children)
        assert all(getattr(child, "custom_id", "") != "fr_enter:1" for child in hidden.children)

    asyncio.run(_build())


def test_host_controls_include_required_actions_and_reroll_state():
    async def _build():
        view = HostControlsView(
            raffle_id=9,
            on_end_now=_noop,
            on_cancel=_noop,
            on_refresh=_noop,
            on_view_entries=_noop,
            on_reroll=_noop,
            can_reroll=False,
        )
        labels = {getattr(child, "label", None): child for child in view.children}
        assert "⏹️ End Giveaway Now" in labels
        assert "❌ Cancel Giveaway" in labels
        assert "🔄 Refresh Panel" in labels
        assert "📋 View Entries" in labels
        assert labels["🎲 Reroll Winner"].disabled is True

    asyncio.run(_build())


def test_giveaway_creation_and_channel_resolution_wired_in_source():
    src = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    assert "Choose the giveaway entry mode and posting channel." in src
    assert "GuildSettingsRepository.resolve_raffle_giveaway_purchase_channel_id" in src
    assert "button_join_enabled" in src
    assert "auto_entry_enabled" in src
    assert "weighted_enabled" in src
    assert "This giveaway uses auto-entry only." in src


def test_raffle_recovery_controls_and_repository_wired_in_source():
    cog_src = Path("cogs/raffles.py").read_text(encoding="utf-8")
    repo_src = Path("repositories/raffles.py").read_text(encoding="utf-8")
    migration_src = Path("migrations/2026_03_18_giveaway_and_raffle_recovery.sql").read_text(
        encoding="utf-8"
    )

    assert "Recreate Canceled Raffle" in cog_src
    assert "recreate_cancelled_raffle" in cog_src
    assert "get_recovery_preview" in repo_src
    assert "superseded_by_raffle_id" in repo_src
    assert "recreated_from_raffle_id" in repo_src
    assert "recreated_from_entry_id" in repo_src
    assert "ADD COLUMN IF NOT EXISTS superseded_by_raffle_id" in migration_src
    assert "ADD COLUMN IF NOT EXISTS recreated_from_entry_id" in migration_src
