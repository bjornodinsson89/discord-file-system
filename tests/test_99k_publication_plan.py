from cogs.events import get_99k_publication_plan


def test_disable_announcement_does_not_disable_panel_upsert():
    plan = get_99k_publication_plan({"disable_99k_announcements": True})
    assert plan["upsert_signup_panel"] is True
    assert plan["post_announcement"] is False


def test_enabled_announcement_keeps_both_paths():
    plan = get_99k_publication_plan({"disable_99k_announcements": False})
    assert plan["upsert_signup_panel"] is True
    assert plan["post_announcement"] is True
