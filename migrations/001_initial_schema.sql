-- Migration 001: Initial Schema
-- Creates all base tables for Happy Jumper Bot

-- User API Keys
CREATE TABLE IF NOT EXISTS user_api_keys (
    discord_id BIGINT PRIMARY KEY,
    torn_user_id INTEGER NOT NULL,
    encrypted_key TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Guild Settings
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    host99k_role_id BIGINT,
    insurer_role_id BIGINT,
    admin_role_id BIGINT,
    jump_99k_channel_id BIGINT,
    insurance_channel_id BIGINT,
    raffle_channel_id BIGINT,
    reservation_timeout_minutes INTEGER DEFAULT 5,
    auto_complete_enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Happy Jump Sessions
CREATE TABLE IF NOT EXISTS happy_jump_sessions (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    host_discord_id BIGINT NOT NULL,
    host_torn_id INTEGER NOT NULL,
    jump_type VARCHAR(20) DEFAULT '99k',
    max_spots INTEGER NOT NULL,
    xanax_count INTEGER NOT NULL,
    start_in_hours INTEGER DEFAULT 0,
    created_tct INTEGER,
    estimated_jump_tct INTEGER,
    payment_type VARCHAR(20) NOT NULL,
    payment_amount INTEGER NOT NULL,
    payment_item_id INTEGER,
    status VARCHAR(20) DEFAULT 'open',
    announcement_message_id BIGINT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sessions_guild_status ON happy_jump_sessions(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_host ON happy_jump_sessions(host_discord_id, status);

-- Jump Signups
CREATE TABLE IF NOT EXISTS happy_jump_signups (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    status VARCHAR(20) DEFAULT 'reserved',
    reserved_until TIMESTAMPTZ,
    signed_up_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_signups_session ON happy_jump_signups(session_id);
CREATE INDEX IF NOT EXISTS idx_signups_expiry ON happy_jump_signups(reserved_until) WHERE status = 'reserved';

-- Jump Waitlist
CREATE TABLE IF NOT EXISTS happy_jump_waitlist (
    id SERIAL PRIMARY KEY,
    session_id INTEGER REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_waitlist_session ON happy_jump_waitlist(session_id, position);

-- Readiness Tracking
CREATE TABLE IF NOT EXISTS happy_jump_readiness (
    session_id INTEGER NOT NULL,
    discord_id BIGINT NOT NULL,
    energy INTEGER DEFAULT 0,
    energy_max INTEGER DEFAULT 150,
    drug_cooldown INTEGER DEFAULT 0,
    status_text VARCHAR(50),
    checked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY(session_id, discord_id)
);

-- Host Reputation
CREATE TABLE IF NOT EXISTS host_reputation (
    discord_id BIGINT PRIMARY KEY,
    torn_id INTEGER,
    sessions_completed INTEGER DEFAULT 0,
    sessions_cancelled INTEGER DEFAULT 0,
    total_participants INTEGER DEFAULT 0,
    average_rating FLOAT,
    total_ratings INTEGER DEFAULT 0,
    positive_ratings INTEGER DEFAULT 0,
    negative_ratings INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Host Ratings
CREATE TABLE IF NOT EXISTS host_ratings (
    id SERIAL PRIMARY KEY,
    host_discord_id BIGINT NOT NULL,
    rater_discord_id BIGINT NOT NULL,
    session_id INTEGER,
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(host_discord_id, rater_discord_id, session_id)
);

-- Blacklist
CREATE TABLE IF NOT EXISTS blacklist (
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    reason TEXT,
    banned_by BIGINT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY(guild_id, discord_id)
);

-- Insurance Providers
CREATE TABLE IF NOT EXISTS insurance_providers (
    provider_id SERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE NOT NULL,
    torn_user_id INTEGER NOT NULL,
    company_name VARCHAR(100),
    verified BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insurance Policies
CREATE TABLE IF NOT EXISTS insurance_policies (
    policy_id SERIAL PRIMARY KEY,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    name VARCHAR(100) NOT NULL,
    description TEXT,
    covered_jump_types TEXT[] DEFAULT ARRAY['99k'],
    max_coverage_xanax INTEGER DEFAULT 100,
    premium_per_xanax INTEGER NOT NULL,
    payout_per_xanax INTEGER NOT NULL,
    duration_hours INTEGER DEFAULT 24,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Insurance Coverage
CREATE TABLE IF NOT EXISTS insurance_coverage (
    coverage_id SERIAL PRIMARY KEY,
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    user_torn_id INTEGER NOT NULL,
    xanax_covered INTEGER NOT NULL,
    premium_paid INTEGER NOT NULL,
    payout_amount INTEGER NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    expires_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_coverage_user ON insurance_coverage(user_discord_id, status);
CREATE INDEX IF NOT EXISTS idx_coverage_expiry ON insurance_coverage(expires_at) WHERE status = 'active';

-- Insurance Claims
CREATE TABLE IF NOT EXISTS insurance_claims (
    claim_id SERIAL PRIMARY KEY,
    coverage_id INTEGER REFERENCES insurance_coverage(coverage_id),
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    claim_type VARCHAR(50) NOT NULL,
    xanax_lost INTEGER NOT NULL,
    payout_amount INTEGER NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    torn_log_evidence TEXT,
    notes TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON insurance_claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_provider ON insurance_claims(provider_id, status);

-- Raffles
CREATE TABLE IF NOT EXISTS raffles (
    raffle_id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    creator_discord_id BIGINT NOT NULL,
    prize TEXT NOT NULL,
    ticket_payment_type VARCHAR(20) NOT NULL,
    ticket_price INTEGER NOT NULL,
    ticket_payment_item_id INTEGER,
    max_tickets INTEGER NOT NULL,
    max_tickets_per_user INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'active',
    winner_discord_id BIGINT,
    winner_torn_id INTEGER,
    end_time TIMESTAMPTZ NOT NULL,
    announcement_message_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_raffles_guild_status ON raffles(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_raffles_end_time ON raffles(end_time) WHERE status = 'active';

-- Raffle Entries
CREATE TABLE IF NOT EXISTS raffle_entries (
    entry_id SERIAL PRIMARY KEY,
    raffle_id INTEGER REFERENCES raffles(raffle_id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    num_tickets INTEGER NOT NULL,
    payment_verified BOOLEAN DEFAULT FALSE,
    reserved_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(raffle_id, discord_id)
);
CREATE INDEX IF NOT EXISTS idx_entries_raffle ON raffle_entries(raffle_id);
CREATE INDEX IF NOT EXISTS idx_entries_expiry ON raffle_entries(reserved_until) WHERE NOT payment_verified;

-- Audit Log
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    actor_discord_id BIGINT,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id INTEGER,
    payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_discord_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

-- Dashboard Sessions
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    discord_id BIGINT NOT NULL,
    session_data JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_expiry ON dashboard_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_dashboard_sessions_discord_id ON dashboard_sessions(discord_id);
