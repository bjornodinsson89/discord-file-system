BEGIN;

CREATE TABLE IF NOT EXISTS public.engagement_profiles (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    xp_total BIGINT NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 0,
    prize_token_balance BIGINT NOT NULL DEFAULT 0,
    prize_token_lifetime_earned BIGINT NOT NULL DEFAULT 0,
    prize_token_lifetime_spent BIGINT NOT NULL DEFAULT 0,
    message_xp_total BIGINT NOT NULL DEFAULT 0,
    reaction_xp_total BIGINT NOT NULL DEFAULT 0,
    voice_xp_total BIGINT NOT NULL DEFAULT 0,
    paid_raffle_xp_total BIGINT NOT NULL DEFAULT 0,
    jump_purchase_xp_total BIGINT NOT NULL DEFAULT 0,
    jump_completion_xp_total BIGINT NOT NULL DEFAULT 0,
    paid_raffle_purchases_count INTEGER NOT NULL DEFAULT 0,
    paid_raffle_tickets_count INTEGER NOT NULL DEFAULT 0,
    jump_99k_purchases_count INTEGER NOT NULL DEFAULT 0,
    jump_99k_completed_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS public.engagement_event_ledger (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    event_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    xp_delta INTEGER NOT NULL DEFAULT 0,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reversed_at TIMESTAMPTZ NULL,
    reversal_reason TEXT NULL,
    UNIQUE (guild_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS public.prize_token_transactions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    transaction_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance_after BIGINT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reversed_at TIMESTAMPTZ NULL,
    reversal_reason TEXT NULL,
    UNIQUE (guild_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS public.engagement_guild_settings (
    guild_id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    levelup_channel_id BIGINT NULL,
    leaderboard_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    profile_cards_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    message_xp_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    reaction_xp_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    voice_xp_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    message_xp_amount INTEGER NOT NULL DEFAULT 12,
    message_xp_cooldown_seconds INTEGER NOT NULL DEFAULT 60,
    reaction_xp_amount INTEGER NOT NULL DEFAULT 2,
    reaction_xp_hourly_cap INTEGER NOT NULL DEFAULT 20,
    voice_xp_per_minute INTEGER NOT NULL DEFAULT 5,
    paid_raffle_purchase_xp_base INTEGER NOT NULL DEFAULT 15,
    paid_raffle_purchase_xp_per_ticket INTEGER NOT NULL DEFAULT 2,
    paid_raffle_purchase_xp_cap INTEGER NOT NULL DEFAULT 50,
    jump_purchase_xp INTEGER NOT NULL DEFAULT 40,
    jump_completion_xp INTEGER NOT NULL DEFAULT 75,
    auto_entry_giveaways_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ignored_channel_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ignored_category_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    ignored_role_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.engagement_message_state (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    last_eligible_message_at TIMESTAMPTZ NULL,
    last_message_fingerprint TEXT NULL,
    last_channel_id BIGINT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS public.engagement_reaction_state (
    guild_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    reactor_user_id BIGINT NOT NULL,
    target_user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, message_id, reactor_user_id)
);

COMMIT;
