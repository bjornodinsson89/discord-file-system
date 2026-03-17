from pathlib import Path


def test_000_full_schema_contains_required_columns():
    schema_sql = Path("migrations/000_full_schema.sql").read_text(encoding="utf-8")

    required_columns = [
        "guild_id BIGINT PRIMARY KEY",
        "announce_channel_id BIGINT",
        "jump_99k_channel_id BIGINT",
        "raffle_channel_id BIGINT",
        "insurance_channel_id BIGINT",
        "welcome_channel_id BIGINT",
        "admin_role_ids JSONB",
        "host99k_role_id BIGINT",
        "insurer_role_id BIGINT",
        "welcome_enabled BOOLEAN DEFAULT FALSE",
        "welcome_message_template TEXT",
        "auto_complete_enabled BOOLEAN DEFAULT TRUE",
        "reservation_timeout_minutes INTEGER DEFAULT 5",
        "updated_at TIMESTAMPTZ DEFAULT NOW()",
    ]

    for column in required_columns:
        assert column in schema_sql, f"Missing required schema column definition: {column}"


def test_005_canonical_jump_99k_pricing_migration_defines_required_columns():
    migration_sql = Path("migrations/005_canonicalize_jump_99k_pricing.sql").read_text(
        encoding="utf-8"
    )

    required_snippets = [
        "ADD COLUMN IF NOT EXISTS price_item TEXT",
        "ADD COLUMN IF NOT EXISTS price_amount INT",
        "ADD COLUMN IF NOT EXISTS scheduled_start_text TEXT",
        "ALTER COLUMN price_item SET NOT NULL",
        "ALTER COLUMN price_amount SET NOT NULL",
        "DROP COLUMN " + "price_" + "quantity",
    ]

    for snippet in required_snippets:
        assert snippet in migration_sql


def test_who_can_jump_panel_migration_adds_required_columns():
    migration_sql = Path("migrations/2026_03_17_add_who_can_jump_panel_columns.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS who_can_jump_channel_id BIGINT" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS who_can_jump_message_id BIGINT" in migration_sql


def test_who_can_jump_page_index_migration_adds_required_column():
    migration_sql = Path("migrations/2026_03_17_add_who_can_jump_page_index.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS who_can_jump_page_index INTEGER NOT NULL DEFAULT 0" in migration_sql
