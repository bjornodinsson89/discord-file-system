-- ============================================================================
-- Happy Jumper Bot - Complete Database Schema
-- Single migration that creates all tables fresh
-- Compatible with Supabase/PostgreSQL
-- ============================================================================

-- Drop all existing tables (for clean rebuild)
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS raffle_entries CASCADE;
DROP TABLE IF EXISTS raffles CASCADE;
DROP TABLE IF EXISTS insurance_claims CASCADE;
DROP TABLE IF EXISTS insurance_coverage CASCADE;
DROP TABLE IF EXISTS insurance_policies CASCADE;
DROP TABLE IF EXISTS insurance_providers CASCADE;
DROP TABLE IF EXISTS blacklist CASCADE;
DROP TABLE IF EXISTS host_ratings CASCADE;
DROP TABLE IF EXISTS host_reputation CASCADE;
DROP TABLE IF EXISTS happy_jump_readiness CASCADE;
DROP TABLE IF EXISTS happy_jump_waitlist CASCADE;
DROP TABLE IF EXISTS happy_jump_signups CASCADE;
DROP TABLE IF EXISTS happy_jump_sessions CASCADE;
DROP TABLE IF EXISTS guild_settings CASCADE;
DROP TABLE IF EXISTS user_api_keys CASCADE;

-- ============================================================================
-- USER API KEYS
-- Stores encrypted Torn API keys linked to Discord users
-- ============================================================================
CREATE TABLE user_api_keys (
    discord_id BIGINT PRIMARY KEY,
    torn_user_id INTEGER NOT NULL,
    encrypted_key TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- GUILD SETTINGS
-- Per-guild configuration for roles, channels, and features
-- ============================================================================
CREATE TABLE guild_settings (
    guild_id BIGINT PRIMARY KEY,
    host99k_role_id BIGINT,
    insurer_role_id BIGINT,
    admin_role_id BIGINT,  -- Permission Model C: users with this role can manage the bot
    jump_99k_channel_id BIGINT,
    insurance_channel_id BIGINT,
    raffle_channel_id BIGINT,
    reservation_timeout_minutes INTEGER DEFAULT 5,
    auto_complete_enabled BOOLEAN DEFAULT TRUE,
    announce_channel_id BIGINT,
    admin_role_ids JSONB,
    welcome_enabled BOOLEAN DEFAULT FALSE,
    welcome_message_template TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- HAPPY JUMP SESSIONS
-- 99k jump hosting sessions
-- ============================================================================
CREATE TABLE happy_jump_sessions (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    host_discord_id BIGINT NOT NULL,
    host_torn_id INTEGER NOT NULL,
    jump_type VARCHAR(20) DEFAULT '99k',
    max_spots INTEGER NOT NULL CHECK (max_spots >= 1 AND max_spots <= 30),
    xanax_count INTEGER NOT NULL DEFAULT 1,  -- Legacy field for backwards compat
    xanax_stack VARCHAR(20) NOT NULL DEFAULT '1_xanax',
    start_in_hours INTEGER DEFAULT 0 CHECK (start_in_hours >= 0 AND start_in_hours <= 72),
    created_tct INTEGER,
    estimated_jump_tct INTEGER,
    payment_type VARCHAR(20) NOT NULL,
    payment_amount INTEGER NOT NULL CHECK (payment_amount >= 1),
    payment_item_id INTEGER,
    status VARCHAR(20) DEFAULT 'open',
    announcement_message_id BIGINT,
    announcement_channel_id BIGINT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_xanax_stack CHECK (xanax_stack IN ('1_xanax', '2_xanax', '3_xanax', 'full_stack')),
    CONSTRAINT chk_payment_type CHECK (payment_type IN ('xanax', 'erotic_dvd')),
    CONSTRAINT chk_session_status CHECK (status IN ('open', 'locked', 'completed', 'cancelled'))
);

CREATE INDEX idx_sessions_guild_status ON happy_jump_sessions(guild_id, status);
CREATE INDEX idx_sessions_host ON happy_jump_sessions(host_discord_id, status);
CREATE INDEX idx_sessions_created_at ON happy_jump_sessions(created_at DESC);

-- ============================================================================
-- JUMP SIGNUPS
-- Users signed up for jump sessions
-- ============================================================================
CREATE TABLE happy_jump_signups (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    status VARCHAR(20) DEFAULT 'reserved',
    reserved_until TIMESTAMPTZ,
    payment_verified BOOLEAN DEFAULT FALSE,
    signed_up_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_signup_status CHECK (status IN ('reserved', 'confirmed', 'completed', 'cancelled')),
    UNIQUE(session_id, discord_id)
);

CREATE INDEX idx_signups_session ON happy_jump_signups(session_id);
CREATE INDEX idx_signups_expiry ON happy_jump_signups(reserved_until) WHERE status = 'reserved';

-- ============================================================================
-- JUMP WAITLIST
-- Waitlist for full sessions
-- ============================================================================
CREATE TABLE happy_jump_waitlist (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(session_id, discord_id)
);

CREATE INDEX idx_waitlist_session ON happy_jump_waitlist(session_id, position);

-- ============================================================================
-- READINESS TRACKING
-- Energy and drug cooldown status for session participants
-- ============================================================================
CREATE TABLE happy_jump_readiness (
    session_id INTEGER NOT NULL,
    discord_id BIGINT NOT NULL,
    energy INTEGER DEFAULT 0,
    energy_max INTEGER DEFAULT 150,
    drug_cooldown INTEGER DEFAULT 0,
    status_text VARCHAR(50),
    checked_at TIMESTAMPTZ DEFAULT NOW(),
    
    PRIMARY KEY(session_id, discord_id)
);

-- ============================================================================
-- HOST REPUTATION
-- Aggregated reputation scores for hosts
-- ============================================================================
CREATE TABLE host_reputation (
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

-- ============================================================================
-- HOST RATINGS
-- Individual ratings from participants
-- ============================================================================
CREATE TABLE host_ratings (
    id SERIAL PRIMARY KEY,
    host_discord_id BIGINT NOT NULL,
    rater_discord_id BIGINT NOT NULL,
    session_id INTEGER,
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    comment TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(host_discord_id, rater_discord_id, session_id)
);

-- ============================================================================
-- BLACKLIST
-- Per-guild blacklisted users
-- ============================================================================
CREATE TABLE blacklist (
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    reason TEXT,
    banned_by BIGINT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    PRIMARY KEY(guild_id, discord_id)
);

-- ============================================================================
-- INSURANCE PROVIDERS
-- Insurance provider accounts
-- ============================================================================
CREATE TABLE insurance_providers (
    provider_id SERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE NOT NULL,
    torn_user_id INTEGER NOT NULL,
    company_name VARCHAR(100),
    guild_id BIGINT,
    verified BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT FALSE,
    approval_status VARCHAR(20) DEFAULT 'pending',
    approved_by BIGINT,
    approved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_approval_status CHECK (approval_status IN ('pending', 'approved', 'rejected', 'disabled'))
);

CREATE INDEX idx_providers_approval_status ON insurance_providers(approval_status);
CREATE INDEX idx_providers_guild ON insurance_providers(guild_id);

-- ============================================================================
-- INSURANCE POLICIES
-- Policy offerings from providers
-- ============================================================================
CREATE TABLE insurance_policies (
    policy_id SERIAL PRIMARY KEY,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    guild_id BIGINT,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    covered_jump_types TEXT[] DEFAULT ARRAY['99k'],
    
    -- Cost structure
    cost_type VARCHAR(20) NOT NULL,
    cost_amount INTEGER NOT NULL CHECK (cost_amount >= 1),
    
    -- Coverage details
    coverage_type VARCHAR(50) NOT NULL,
    max_coverage_xanax INTEGER DEFAULT 100,
    payout_description TEXT,
    
    -- Legacy fields (kept for backwards compatibility)
    premium_per_xanax INTEGER,
    payout_per_xanax INTEGER,
    
    duration_hours INTEGER DEFAULT 24 CHECK (duration_hours >= 1 AND duration_hours <= 720),
    active BOOLEAN DEFAULT TRUE,
    
    announcement_message_id BIGINT,
    announcement_channel_id BIGINT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_cost_type CHECK (cost_type IN ('xanax', 'erotic_dvd')),
    CONSTRAINT chk_coverage_type CHECK (coverage_type IN ('xanax_stack', 'ecstasy_after_stack', 'all_drugs'))
);

CREATE INDEX idx_policies_provider_active ON insurance_policies(provider_id, active);
CREATE INDEX idx_policies_guild ON insurance_policies(guild_id, active);

-- ============================================================================
-- INSURANCE COVERAGE
-- Active coverage purchased by users
-- ============================================================================
CREATE TABLE insurance_coverage (
    coverage_id SERIAL PRIMARY KEY,
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    user_torn_id INTEGER NOT NULL,
    xanax_covered INTEGER NOT NULL,
    premium_paid INTEGER NOT NULL,
    premium_type VARCHAR(20) NOT NULL DEFAULT 'xanax',
    payout_amount INTEGER NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    expires_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_coverage_status CHECK (status IN ('pending', 'active', 'expired', 'claimed')),
    CONSTRAINT chk_premium_type CHECK (premium_type IN ('xanax', 'erotic_dvd'))
);

CREATE INDEX idx_coverage_user ON insurance_coverage(user_discord_id, status);
CREATE INDEX idx_coverage_expiry ON insurance_coverage(expires_at) WHERE status = 'active';
CREATE INDEX idx_coverage_policy ON insurance_coverage(policy_id);

-- ============================================================================
-- INSURANCE CLAIMS
-- Claims filed against coverage
-- ============================================================================
CREATE TABLE insurance_claims (
    claim_id SERIAL PRIMARY KEY,
    coverage_id INTEGER REFERENCES insurance_coverage(coverage_id),
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    claim_type VARCHAR(50) NOT NULL,
    xanax_lost INTEGER NOT NULL,
    payout_amount INTEGER NOT NULL,
    payout_type VARCHAR(20) DEFAULT 'xanax',
    status VARCHAR(20) DEFAULT 'pending',
    torn_log_id BIGINT,
    torn_log_evidence TEXT,
    notes TEXT,
    resolved_by BIGINT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_claim_status CHECK (status IN ('pending', 'approved', 'rejected', 'paid')),
    CONSTRAINT chk_claim_type CHECK (claim_type IN ('overdose', 'drug_loss', 'other'))
);

CREATE INDEX idx_claims_status ON insurance_claims(status);
CREATE INDEX idx_claims_provider ON insurance_claims(provider_id, status);
CREATE INDEX idx_claims_user ON insurance_claims(user_discord_id);

-- ============================================================================
-- RAFFLES
-- Raffle events
-- ============================================================================
CREATE TABLE raffles (
    raffle_id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    creator_discord_id BIGINT NOT NULL,
    prize TEXT NOT NULL,
    ticket_payment_type VARCHAR(20) NOT NULL,
    ticket_price INTEGER NOT NULL CHECK (ticket_price >= 1),
    ticket_payment_item_id INTEGER,
    tickets_available INTEGER NOT NULL CHECK (tickets_available >= 10),
    max_tickets INTEGER,  -- Legacy field (kept for backwards compat)
    max_tickets_per_user INTEGER DEFAULT 0,  -- 0 = unlimited
    status VARCHAR(20) DEFAULT 'active',
    winner_discord_id BIGINT,
    winner_torn_id INTEGER,
    winning_ticket_number INTEGER,
    end_time TIMESTAMPTZ NOT NULL,
    announcement_message_id BIGINT,
    announcement_channel_id BIGINT,
    drawn_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT chk_ticket_payment_type CHECK (ticket_payment_type IN ('xanax', 'erotic_dvd')),
    CONSTRAINT chk_raffle_status CHECK (status IN ('active', 'completed', 'cancelled'))
);

CREATE INDEX idx_raffles_guild_status ON raffles(guild_id, status);
CREATE INDEX idx_raffles_end_time ON raffles(end_time) WHERE status = 'active';
CREATE INDEX idx_raffles_created_at ON raffles(created_at DESC);
CREATE INDEX idx_raffles_creator ON raffles(creator_discord_id, status);

-- ============================================================================
-- RAFFLE ENTRIES
-- Tickets purchased for raffles
-- ============================================================================
CREATE TABLE raffle_entries (
    entry_id SERIAL PRIMARY KEY,
    raffle_id INTEGER NOT NULL REFERENCES raffles(raffle_id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    num_tickets INTEGER NOT NULL CHECK (num_tickets >= 1),
    ticket_start INTEGER,  -- First ticket number in this entry
    ticket_end INTEGER,    -- Last ticket number in this entry
    payment_verified BOOLEAN DEFAULT FALSE,
    reserved_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(raffle_id, discord_id)
);

CREATE INDEX idx_entries_raffle ON raffle_entries(raffle_id);
CREATE INDEX idx_entries_expiry ON raffle_entries(reserved_until) WHERE NOT payment_verified;

-- ============================================================================
-- AUDIT LOG
-- All admin actions for accountability
-- ============================================================================
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,
    actor_discord_id BIGINT,
    actor_torn_id INTEGER,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),
    target_id BIGINT,
    payload JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_guild ON audit_log(guild_id);
CREATE INDEX idx_audit_actor ON audit_log(actor_discord_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX idx_audit_target ON audit_log(target_type, target_id);


-- ============================================================================
-- HELPER FUNCTIONS
-- ============================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Apply updated_at triggers
CREATE TRIGGER update_user_api_keys_updated_at BEFORE UPDATE ON user_api_keys FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_guild_settings_updated_at BEFORE UPDATE ON guild_settings FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
CREATE TRIGGER update_sessions_updated_at BEFORE UPDATE ON happy_jump_sessions FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VERIFICATION QUERIES
-- Run these to verify schema is correct
-- ============================================================================
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;
-- SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'happy_jump_sessions' ORDER BY ordinal_position;
-- SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'raffles' ORDER BY ordinal_position;
-- SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'insurance_policies' ORDER BY ordinal_position;
