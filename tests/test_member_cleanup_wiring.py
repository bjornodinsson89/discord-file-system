from pathlib import Path


def test_member_cleanup_wired_to_member_remove_and_reconciliation_worker():
    src = Path("cogs/events.py").read_text(encoding="utf-8")
    assert "async def on_member_remove" in src
    assert "MemberCleanupService" in src
    assert "departed_member_reconciliation_worker" in src
