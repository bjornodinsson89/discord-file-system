ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS announce_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS admin_role_ids JSONB,
    ADD COLUMN IF NOT EXISTS welcome_enabled BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS welcome_message_template TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE guild_settings
SET announce_channel_id = COALESCE(announce_channel_id, jump_99k_channel_id, insurance_channel_id, raffle_channel_id)
WHERE announce_channel_id IS NULL;
