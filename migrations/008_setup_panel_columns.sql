ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS welcome_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS session_announce_template TEXT;

UPDATE guild_settings
SET announce_channel_id = COALESCE(announce_channel_id, jump_99k_channel_id)
WHERE announce_channel_id IS NULL;

UPDATE guild_settings
SET jump_99k_channel_id = COALESCE(jump_99k_channel_id, announce_channel_id)
WHERE jump_99k_channel_id IS NULL;
