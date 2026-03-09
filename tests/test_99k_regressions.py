from pathlib import Path


def test_modal_submit_defers_and_uses_followup_for_success():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert "await interaction.response.defer(ephemeral=True, thinking=True)" in events_py
    assert (
        'await interaction.followup.send(embed=create_success_embed("99k session saved"'
        in events_py
    )


def test_transition_refresh_uses_repost_helper_not_interaction_message_edit():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert (
        "refreshed = await _refresh_or_repost_roster_panel(interaction.client, self.session_id)"
        in events_py
    )
    assert "await _refresh_99k_panel(interaction.client, self.session_id)" in events_py
    transition_block = events_py.split("def _build_transition_handler", 1)[1].split(
        "async def _on_refresh", 1
    )[0]
    assert (
        "await _refresh_roster_panel(self.session_id, interaction.channel, interaction.message)"
        not in transition_block
    )


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
