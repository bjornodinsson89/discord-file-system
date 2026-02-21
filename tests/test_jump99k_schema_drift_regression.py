from pathlib import Path


def test_pending_signup_and_insurance_queries_match_new_schema_migration_columns():
    jumps_repo = Path("repositories/jumps.py").read_text(encoding="utf-8")
    migration = Path("migrations/2026_02_21_add_missing_columns.sql").read_text(encoding="utf-8")

    assert "s.participant_torn_name" in jumps_repo
    assert "requested_at" in jumps_repo

    assert "ADD COLUMN IF NOT EXISTS participant_torn_name TEXT" in migration
    assert "ADD COLUMN IF NOT EXISTS requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()" in migration
