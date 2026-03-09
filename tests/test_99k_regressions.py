from pathlib import Path


def test_modal_submit_defers_and_uses_followup_for_success():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert "await interaction.response.defer(ephemeral=True, thinking=True)" in events_py
    assert (
        'await interaction.followup.send(embed=create_success_embed("99k session saved"'
        in events_py
    )


def test_roster_refresh_uses_repost_helper():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert (
        "refreshed = await _refresh_or_repost_roster_panel(interaction.client, self.session_id)"
        in events_py
    )
    assert "await _refresh_99k_panel(interaction.client, self.session_id)" in events_py


def test_needs_cleanup_status_is_allowed_by_migration_and_repo_queries():
    migration_sql = Path("migrations/2026_03_09_allow_needs_cleanup_session_status.sql").read_text(
        encoding="utf-8"
    )
    jumps_repo = Path("repositories/jumps.py").read_text(encoding="utf-8")

    assert "'needs_cleanup'" in migration_sql
    assert 'await repo.update_session_status(int(session_id), "needs_cleanup")' in Path(
        "cogs/events.py"
    ).read_text(encoding="utf-8")
    assert (
        "WHERE status IN ('closed', 'cancelled', 'expired', 'completed', 'needs_cleanup')"
        in jumps_repo
    )


def test_host_controls_are_host_only_and_use_ephemeral_message_path():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    assert "Only the jump host can use these controls." in events_py
    assert "99k_roster_host_controls" in events_py
    assert 'label="Host Controls"' in events_py


def test_jump_automation_worker_runs_every_3_seconds():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    assert "@tasks.loop(seconds=3)" in events_py
    assert "async def jump_automation_worker" in events_py
