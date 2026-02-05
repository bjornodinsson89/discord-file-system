-- Upgrade audit_log target_id to BIGINT for Discord snowflakes

ALTER TABLE audit_log
    ALTER COLUMN target_id TYPE BIGINT;

INSERT INTO schema_migrations (version, description)
VALUES (3, 'Expand audit_log.target_id to BIGINT for Discord IDs')
ON CONFLICT (version) DO NOTHING;
