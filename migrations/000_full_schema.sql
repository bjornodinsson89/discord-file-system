CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    announce_channel_id BIGINT,
    jump_99k_channel_id BIGINT,
    raffle_channel_id BIGINT,
    insurance_channel_id BIGINT,
    welcome_channel_id BIGINT,
    admin_role_ids JSONB,
    host99k_role_id BIGINT,
    insurer_role_id BIGINT,
    welcome_enabled BOOLEAN DEFAULT FALSE,
    welcome_message_template TEXT,
    auto_complete_enabled BOOLEAN DEFAULT TRUE,
    reservation_timeout_minutes INTEGER DEFAULT 5,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
