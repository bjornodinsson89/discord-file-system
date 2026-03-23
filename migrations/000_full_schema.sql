BEGIN;

CREATE TABLE IF NOT EXISTS public.guild_settings (
    guild_id BIGINT PRIMARY KEY,
    announce_channel_id BIGINT,
    jump_99k_channel_id BIGINT,
    raffle_channel_id BIGINT,
    insurance_channel_id BIGINT,
    welcome_channel_id BIGINT,
    admin_role_ids JSONB,
    admin_key_strategy TEXT NOT NULL DEFAULT 'pool' CHECK (admin_key_strategy IN ('pool', 'single')),
    admin_key_single_discord_id BIGINT,
    host99k_role_id BIGINT,
    insurer_role_id BIGINT,
    jump_announce_channel_id BIGINT,
    jump_99k_private_category_id BIGINT,
    welcome_enabled BOOLEAN DEFAULT FALSE,
    welcome_message_template TEXT,
    auto_complete_enabled BOOLEAN DEFAULT TRUE,
    reservation_timeout_minutes INTEGER DEFAULT 5,
    disable_99k_announcements BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.user_api_keys (
    discord_id BIGINT PRIMARY KEY,
    torn_user_id BIGINT,
    torn_name TEXT,
    encrypted_key TEXT NOT NULL,
    timezone_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.jump_99k_sessions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    host_discord_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    scheduled_start_text TEXT,
    start_time TIMESTAMPTZ,
    max_slots INTEGER NOT NULL DEFAULT 7,
    notes TEXT,
    price_item TEXT NOT NULL DEFAULT 'xanax',
    price_amount INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',
    announce_channel_id BIGINT,
    announce_message_id BIGINT,
    private_channel_id BIGINT,
    roster_channel_id BIGINT,
    roster_message_id BIGINT,
    host_controls_channel_id BIGINT,
    host_controls_message_id BIGINT,
    signups_locked BOOLEAN NOT NULL DEFAULT FALSE,
    cleaned_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.jump_99k_signups (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    participant_discord_id BIGINT,
    participant_torn_user_id BIGINT,
    participant_torn_name TEXT,
    status TEXT NOT NULL DEFAULT 'reserved',
    payment_verified BOOLEAN NOT NULL DEFAULT FALSE,
    payment_verified_at TIMESTAMPTZ,
    payment_source TEXT,
    reserved_until TIMESTAMPTZ,
    is_priority BOOLEAN NOT NULL DEFAULT FALSE,
    signed_up_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, participant_discord_id),
    CONSTRAINT jump_99k_signups_status_check CHECK (status IN ('reserved', 'paid', 'cancelled', 'expired', 'completed', 'not_completed'))
);

CREATE TABLE IF NOT EXISTS public.jump_99k_cleanup_tasks (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    session_id BIGINT NOT NULL,
    task_type TEXT NOT NULL CHECK (task_type IN ('delete_message', 'delete_channel')),
    channel_id BIGINT,
    message_id BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT jump_99k_cleanup_tasks_unique_task UNIQUE (session_id, task_type, channel_id, message_id)
);

CREATE TABLE IF NOT EXISTS public.raffles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    host_discord_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.free_raffles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT,
    message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_session_participant ON public.jump_99k_signups (session_id, participant_discord_id);

COMMIT;
