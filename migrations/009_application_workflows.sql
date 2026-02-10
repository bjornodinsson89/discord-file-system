ALTER TABLE insurance_providers
    ADD COLUMN IF NOT EXISTS guild_id BIGINT,
    ADD COLUMN IF NOT EXISTS application_data JSONB,
    ADD COLUMN IF NOT EXISTS denial_reason TEXT;

CREATE TABLE IF NOT EXISTS host_applications (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    forum_url TEXT NOT NULL,
    application_data JSONB,
    approval_status VARCHAR(20) DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_by BIGINT,
    approved_at TIMESTAMPTZ,
    denial_reason TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_host_applications_guild_status
ON host_applications(guild_id, approval_status);
