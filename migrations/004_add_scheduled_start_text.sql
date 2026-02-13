-- Adds scheduled_start_text for older deployments where jump_99k_sessions was created without it.
ALTER TABLE jump_99k_sessions
    ADD COLUMN IF NOT EXISTS scheduled_start_text TEXT;
