-- Add guild_id tracking for API key registrations for per-guild user resolution.
ALTER TABLE user_api_keys
    ADD COLUMN IF NOT EXISTS guild_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_user_api_keys_guild_id
    ON user_api_keys(guild_id);
