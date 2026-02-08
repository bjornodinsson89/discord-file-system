-- Migration 006: Welcome message settings per guild

ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS welcome_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS welcome_message_template TEXT,
    ADD COLUMN IF NOT EXISTS welcome_enabled BOOLEAN DEFAULT FALSE;

UPDATE guild_settings
SET welcome_enabled = FALSE
WHERE welcome_enabled IS NULL;
