from pathlib import Path

from utils.embeds import clamp_percent, format_remaining_time, render_text_progress_bar


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
