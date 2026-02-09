from pathlib import Path


def test_000_full_schema_contains_required_columns():
    schema_sql = Path("migrations/000_full_schema.sql").read_text(encoding="utf-8")

    required_columns = [
        "announce_channel_id BIGINT",
        "admin_role_ids JSONB",
        "welcome_enabled BOOLEAN DEFAULT FALSE",
        "welcome_message_template TEXT",
        "payment_verified_at TIMESTAMPTZ",
        "tickets_sold INTEGER DEFAULT 0",
        "torn_log_timestamp INTEGER",
    ]

    for column in required_columns:
        assert column in schema_sql, f"Missing required schema column definition: {column}"

    assert "last_accessed_at" not in schema_sql, "Deprecated dashboard column should not exist in full schema"
