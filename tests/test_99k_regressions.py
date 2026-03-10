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
    assert "async def _refresh_stored_roster_panel_message" in events_py


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
    assert "await interaction.response.send_message(" in events_py
    assert "await interaction.followup.send(" in events_py
    assert "view=Jump99kHostControlsView(session_id=self.session_id)" in events_py


def test_jump_automation_worker_runs_every_3_seconds():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    assert "@tasks.loop(seconds=3)" in events_py
    assert "async def jump_automation_worker" in events_py


def test_host_controls_helpers_are_defined_and_used():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert "async def _safe_defer_ephemeral" in events_py
    assert "async def _safe_edit_original(" in events_py
    assert "await _safe_defer_ephemeral(interaction)" in events_py
    assert "await _safe_edit_original(interaction" in events_py


def test_roster_panel_host_controls_does_not_edit_original_message():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    on_host_controls_block = events_py.split("async def _on_host_controls", 1)[1].split(
        "async def _on_refresh", 1
    )[0]

    assert "_safe_edit_original" not in on_host_controls_block
    assert "interaction.response.send_message" in on_host_controls_block


def test_roster_refresh_uses_single_roster_refresh_path():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    roster_view_block = events_py.split("class Jump99kRosterPanelView", 1)[1].split(
        "async def _end_99k_session_via_shared_flow", 1
    )[0]
    on_refresh_block = roster_view_block.split("async def _on_refresh", 1)[1]

    assert "_refresh_or_repost_roster_panel" in on_refresh_block
    assert "_refresh_roster_if_exists" not in on_refresh_block


def test_roster_refresh_reposts_only_when_stored_message_missing():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    helper_block = events_py.split("async def _refresh_or_repost_roster_panel", 1)[1].split(
        "async def _session_jump_started", 1
    )[0]

    assert 'if refresh_status == "refreshed":' in helper_block
    assert 'if refresh_status == "error":' in helper_block
    assert "await repo.set_roster_panel_message(" in helper_block


def test_delete_button_and_command_share_end_flow():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert "async def _end_99k_session_via_shared_flow(" in events_py
    assert "await _end_99k_session_via_shared_flow(" in events_py
    confirm_block = events_py.split("class Jump99kDeleteConfirmView", 1)[1].split(
        "class Jump99kUserControlsView", 1
    )[0]
    assert "close_session_and_record" not in confirm_block


def test_session_setup_does_not_auto_post_host_controls_message():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")

    assert 'title="Host Controls"' not in events_py
    assert "await repo.set_host_controls_message(" not in events_py


def test_jump_automation_worker_uses_safe_send_channel_signature_with_guild_and_channel_id():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    worker_block = events_py.split("async def jump_automation_worker", 1)[1].split("@jump_automation_worker.before_loop", 1)[0]
    assert "await safe_send_channel(guild, int(channel_id), content=content)" in worker_block
    assert "await safe_send_channel(guild, int(channel_id), content=\"✅ Jump session complete.\")" in worker_block


def test_end_flow_signup_panel_delete_has_fallback_lookup_for_stale_message_ids():
    events_py = Path("cogs/events.py").read_text(encoding="utf-8")
    helper_block = events_py.split("async def _delete_99k_signup_panel_with_fallback", 1)[1].split("async def _grant_private_channel_access", 1)[0]

    assert "delete_message_safe(" in helper_block
    assert "result[1] == \"already_deleted\"" in helper_block
    assert "channel.history(limit=50)" in helper_block
    assert "Session #" in helper_block
