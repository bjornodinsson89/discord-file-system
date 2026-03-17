from pathlib import Path


def test_panel_safe_edit_wired_in_hot_paths():
    events = Path("cogs/events.py").read_text(encoding="utf-8")
    raffles = Path("cogs/raffles.py").read_text(encoding="utf-8")
    free_raffle = Path("cogs/free_raffle.py").read_text(encoding="utf-8")
    pools = Path("cogs/pools.py").read_text(encoding="utf-8")

    assert "PANEL_EDIT_SAFETY.request_edit" in events
    assert "min_interval_seconds=20" in events
    assert "min_interval_seconds=10" in events
    assert "PANEL_EDIT_SAFETY.request_edit" in raffles
    assert "PANEL_EDIT_SAFETY.request_edit" in free_raffle
    assert "PANEL_EDIT_SAFETY.request_edit" in pools
