-- ============================================================================
-- Happy Jumper Bot - Complete Production Schema
-- ============================================================================
-- This is a consolidated schema for fresh deployments.
-- Run this ONCE on a clean database. For existing deployments, use incremental migrations.
-- ============================================================================

-- Drop existing tables if doing a full reset (comment out for production!)
-- DROP TABLE IF EXISTS audit_log CASCADE;
-- DROP TABLE IF EXISTS raffle_entries CASCADE;
-- DROP TABLE IF EXISTS raffles CASCADE;
-- DROP TABLE IF EXISTS insurance_claims CASCADE;
-- DROP TABLE IF EXISTS insurance_coverage CASCADE;
-- DROP TABLE IF EXISTS insurance_policies CASCADE;
-- DROP TABLE IF EXISTS insurance_providers CASCADE;
-- DROP TABLE IF EXISTS blacklist CASCADE;
-- DROP TABLE IF EXISTS host_ratings CASCADE;
-- DROP TABLE IF EXISTS host_reputation CASCADE;
-- DROP TABLE IF EXISTS happy_jump_readiness CASCADE;
-- DROP TABLE IF EXISTS happy_jump_waitlist CASCADE;
-- DROP TABLE IF EXISTS happy_jump_signups CASCADE;
-- DROP TABLE IF EXISTS happy_jump_sessions CASCADE;
-- DROP TABLE IF EXISTS guild_settings CASCADE;
-- DROP TABLE IF EXISTS user_api_keys CASCADE;

-- ============================================================================
-- USER API KEYS
-- Stores encrypted Torn API keys for users
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_api_keys (
    discord_id BIGINT PRIMARY KEY,
    torn_user_id INTEGER NOT NULL,
    encrypted_key TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- GUILD SETTINGS
-- Per-server configuration for channels, roles, and features
-- ============================================================================
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    -- Role configuration
    host99k_role_id BIGINT,              -- Role required to host 99k sessions
    insurer_role_id BIGINT,               -- Role required to be insurance provider
    admin_role_id BIGINT,                 -- Role that grants bot admin access (Permission Model C)
    -- Channel configuration
    jump_99k_channel_id BIGINT,           -- Channel for 99k session announcements
    insurance_channel_id BIGINT,          -- Channel for insurance policy announcements
    raffle_channel_id BIGINT,             -- Channel for raffle announcements
    welcome_channel_id BIGINT,            -- Channel for welcome announcements
    announce_channel_id BIGINT,           -- Unified/legacy announcement channel
    -- Settings
    admin_role_ids JSONB,                 -- Multi-role bot admin access list
    welcome_enabled BOOLEAN DEFAULT FALSE,
    welcome_message_template TEXT,
    reservation_timeout_minutes INTEGER DEFAULT 5,
    auto_complete_enabled BOOLEAN DEFAULT TRUE,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- HAPPY JUMP SESSIONS
-- 99k jump hosting sessions
-- ============================================================================
CREATE TABLE IF NOT EXISTS happy_jump_sessions (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    host_discord_id BIGINT NOT NULL,
    host_torn_id INTEGER NOT NULL,
    -- Session configuration
    jump_type VARCHAR(20) DEFAULT '99k',
    max_spots INTEGER NOT NULL CHECK (max_spots >= 1 AND max_spots <= 30),
    xanax_count INTEGER NOT NULL CHECK (xanax_count >= 1 AND xanax_count <= 4),
    start_in_hours INTEGER DEFAULT 0,
    created_tct INTEGER,                  -- Torn City Time when created
    estimated_jump_tct INTEGER,           -- Estimated jump time in TCT
    -- Payment configuration (ONLY xanax or erotic_dvd allowed)
    payment_type VARCHAR(20) NOT NULL CHECK (payment_type IN ('xanax', 'erotic_dvd')),
    payment_amount INTEGER NOT NULL CHECK (payment_amount >= 1),
    payment_item_id INTEGER,              -- 206 for xanax, 366 for DVD
    -- Status tracking
    status VARCHAR(20) DEFAULT 'open' CHECK (status IN ('open', 'locked', 'completed', 'cancelled')),
    announcement_message_id BIGINT,
    completed_at TIMESTAMPTZ,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_guild_status ON happy_jump_sessions(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_host ON happy_jump_sessions(host_discord_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON happy_jump_sessions(created_at DESC);

-- ============================================================================
-- HAPPY JUMP SIGNUPS
-- Users who have reserved or confirmed spots in sessions
-- ============================================================================
CREATE TABLE IF NOT EXISTS happy_jump_signups (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    status VARCHAR(20) DEFAULT 'reserved' CHECK (status IN ('reserved', 'confirmed', 'completed', 'no_show')),
    reserved_until TIMESTAMPTZ,           -- Expiry for unconfirmed reservations
    payment_verified BOOLEAN DEFAULT FALSE,
    payment_verified_at TIMESTAMPTZ,
    signed_up_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_signups_session ON happy_jump_signups(session_id);
CREATE INDEX IF NOT EXISTS idx_signups_expiry ON happy_jump_signups(reserved_until) WHERE status = 'reserved';

-- ============================================================================
-- HAPPY JUMP WAITLIST
-- Users waiting for a spot to open up
-- ============================================================================
CREATE TABLE IF NOT EXISTS happy_jump_waitlist (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES happy_jump_sessions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    added_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(session_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_waitlist_session ON happy_jump_waitlist(session_id, position);

-- ============================================================================
-- HAPPY JUMP READINESS
-- Real-time energy/drug cooldown tracking for confirmed participants
-- ============================================================================
CREATE TABLE IF NOT EXISTS happy_jump_readiness (
    session_id INTEGER NOT NULL,
    discord_id BIGINT NOT NULL,
    energy INTEGER DEFAULT 0,
    energy_max INTEGER DEFAULT 150,
    drug_cooldown INTEGER DEFAULT 0,      -- Seconds remaining
    status_text VARCHAR(50),              -- Human-readable status
    checked_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY(session_id, discord_id)
);

-- ============================================================================
-- HOST REPUTATION
-- Aggregate reputation metrics for hosts
-- ============================================================================
CREATE TABLE IF NOT EXISTS host_reputation (
    discord_id BIGINT PRIMARY KEY,
    torn_id INTEGER,
    sessions_completed INTEGER DEFAULT 0,
    sessions_cancelled INTEGER DEFAULT 0,
    total_participants INTEGER DEFAULT 0,
    average_rating FLOAT,
    total_ratings INTEGER DEFAULT 0,
    positive_ratings INTEGER DEFAULT 0,   -- 4-5 stars
    negative_ratings INTEGER DEFAULT 0,   -- 1-2 stars
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================================
-- HOST RATINGS
-- Individual ratings from participants
-- ============================================================================
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

-- ============================================================================
-- BLACKLIST
-- Users banned from participating in a guild
-- ============================================================================
CREATE TABLE IF NOT EXISTS blacklist (
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    reason TEXT,
    banned_by BIGINT,
    expires_at TIMESTAMPTZ,               -- NULL = permanent
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY(guild_id, discord_id)
);

-- ============================================================================
-- INSURANCE PROVIDERS
-- Users/companies offering insurance policies
-- ============================================================================
CREATE TABLE IF NOT EXISTS insurance_providers (
    provider_id SERIAL PRIMARY KEY,
    discord_id BIGINT UNIQUE NOT NULL,
    torn_user_id INTEGER NOT NULL,
    company_name VARCHAR(100),
    -- Status
    verified BOOLEAN DEFAULT FALSE,
    active BOOLEAN DEFAULT FALSE,
    approval_status VARCHAR(20) DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected', 'disabled')),
    approved_by BIGINT,
    approved_at TIMESTAMPTZ,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_providers_approval_status ON insurance_providers(approval_status);

-- ============================================================================
-- INSURANCE POLICIES
-- Coverage plans offered by providers
-- ============================================================================
CREATE TABLE IF NOT EXISTS insurance_policies (
    policy_id SERIAL PRIMARY KEY,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    guild_id BIGINT,                      -- Guild where policy is available
    name VARCHAR(100) NOT NULL,
    description TEXT,
    -- Cost configuration (ONLY xanax or erotic_dvd allowed)
    cost_type VARCHAR(20) CHECK (cost_type IN ('xanax', 'erotic_dvd')),
    cost_amount INTEGER CHECK (cost_amount >= 1),
    -- Legacy fields (for backward compatibility)
    covered_jump_types TEXT[] DEFAULT ARRAY['99k'],
    max_coverage_xanax INTEGER DEFAULT 100,
    premium_per_xanax INTEGER,
    payout_per_xanax INTEGER,
    -- New fields
    coverage_type VARCHAR(50) CHECK (coverage_type IN ('xanax', 'ecstasy_after_stack', 'all_drugs')),
    payout_description TEXT,
    duration_hours INTEGER DEFAULT 24 CHECK (duration_hours >= 1 AND duration_hours <= 720),
    -- Status
    active BOOLEAN DEFAULT TRUE,
    announcement_message_id BIGINT,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policies_provider_active ON insurance_policies(provider_id, active);

-- ============================================================================
-- INSURANCE COVERAGE
-- Active coverage purchased by users
-- ============================================================================
CREATE TABLE IF NOT EXISTS insurance_coverage (
    coverage_id SERIAL PRIMARY KEY,
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    user_torn_id INTEGER NOT NULL,
    -- Coverage details
    xanax_covered INTEGER NOT NULL,
    premium_paid INTEGER NOT NULL,
    payout_amount INTEGER NOT NULL,
    -- Status
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'expired', 'claimed', 'cancelled')),
    -- Timing
    reserved_until TIMESTAMPTZ,           -- For payment verification
    expires_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    -- Last check tracking (for monitoring)
    last_log_timestamp INTEGER,           -- Last Torn log timestamp checked
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_coverage_user ON insurance_coverage(user_discord_id, status);
CREATE INDEX IF NOT EXISTS idx_coverage_expiry ON insurance_coverage(expires_at) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_coverage_policy ON insurance_coverage(policy_id);

-- ============================================================================
-- INSURANCE CLAIMS
-- Claims filed against coverage
-- ============================================================================
CREATE TABLE IF NOT EXISTS insurance_claims (
    claim_id SERIAL PRIMARY KEY,
    coverage_id INTEGER REFERENCES insurance_coverage(coverage_id),
    policy_id INTEGER REFERENCES insurance_policies(policy_id),
    user_discord_id BIGINT NOT NULL,
    provider_id INTEGER REFERENCES insurance_providers(provider_id),
    -- Claim details
    claim_type VARCHAR(50) NOT NULL CHECK (claim_type IN ('overdose', 'drug_loss', 'xanax_overdose', 'ecstasy_overdose')),
    xanax_lost INTEGER NOT NULL,
    payout_amount INTEGER NOT NULL,
    -- Evidence
    torn_log_id INTEGER,                  -- Torn API log ID
    torn_log_timestamp INTEGER,           -- Torn API log timestamp
    torn_log_evidence TEXT,               -- JSON dump of log entry
    -- Status
    status VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'paid')),
    notes TEXT,
    -- Resolution
    resolved_by BIGINT,
    resolved_at TIMESTAMPTZ,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_claims_status ON insurance_claims(status);
CREATE INDEX IF NOT EXISTS idx_claims_provider ON insurance_claims(provider_id, status);
CREATE INDEX IF NOT EXISTS idx_claims_coverage ON insurance_claims(coverage_id);

-- ============================================================================
-- RAFFLES
-- Prize raffles with ticket purchases
-- ============================================================================
CREATE TABLE IF NOT EXISTS raffles (
    raffle_id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    creator_discord_id BIGINT NOT NULL,
    -- Prize
    prize TEXT NOT NULL,
    prize_value INTEGER,                  -- Optional: estimated value
    -- Ticket configuration (ONLY xanax or erotic_dvd allowed)
    ticket_payment_type VARCHAR(20) NOT NULL CHECK (ticket_payment_type IN ('xanax', 'erotic_dvd')),
    ticket_price INTEGER NOT NULL CHECK (ticket_price >= 1),
    ticket_payment_item_id INTEGER,       -- 206 for xanax, 366 for DVD
    -- Ticket limits
    max_tickets INTEGER,                  -- Legacy field
    tickets_available INTEGER NOT NULL CHECK (tickets_available >= 10),
    max_tickets_per_user INTEGER DEFAULT 0,  -- 0 = unlimited
    tickets_sold INTEGER DEFAULT 0,
    -- Status
    status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'cancelled', 'drawing')),
    -- Winner
    winner_discord_id BIGINT,
    winner_torn_id INTEGER,
    winner_ticket_number INTEGER,
    -- Timing
    end_time TIMESTAMPTZ NOT NULL,
    drawn_at TIMESTAMPTZ,
    -- Discord message
    announcement_message_id BIGINT,
    announcement_channel_id BIGINT,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raffles_guild_status ON raffles(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_raffles_end_time ON raffles(end_time) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_raffles_created_at ON raffles(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_raffles_creator ON raffles(creator_discord_id, status);

-- ============================================================================
-- RAFFLE ENTRIES
-- Ticket purchases for raffles
-- ============================================================================
CREATE TABLE IF NOT EXISTS raffle_entries (
    entry_id SERIAL PRIMARY KEY,
    raffle_id INTEGER NOT NULL REFERENCES raffles(raffle_id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    torn_user_id INTEGER,
    -- Tickets
    num_tickets INTEGER NOT NULL CHECK (num_tickets >= 1),
    ticket_start INTEGER,                 -- First ticket number
    ticket_end INTEGER,                   -- Last ticket number
    -- Payment
    payment_verified BOOLEAN DEFAULT FALSE,
    payment_verified_at TIMESTAMPTZ,
    reserved_until TIMESTAMPTZ,           -- Expiry for unverified entries
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(raffle_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_raffle ON raffle_entries(raffle_id);
CREATE INDEX IF NOT EXISTS idx_entries_expiry ON raffle_entries(reserved_until) WHERE NOT payment_verified;

-- ============================================================================
-- AUDIT LOG
-- Track all admin actions
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT,                      -- NULL for global actions
    actor_discord_id BIGINT,
    actor_type VARCHAR(20) DEFAULT 'user' CHECK (actor_type IN ('user', 'system', 'bot')),
    -- Action details
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50),              -- 'session', 'raffle', 'policy', 'provider', 'user', 'settings'
    target_id BIGINT,
    -- Payload
    payload JSONB,
    -- Source tracking
    source VARCHAR(20) DEFAULT 'discord' CHECK (source IN ('discord', 'api', 'system')),
    ip_address INET,
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log(guild_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_discord_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id);


-- ============================================================================
-- MIGRATION TRACKING
-- Track which migrations have been applied
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(50) PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW(),
    description TEXT
);

-- Record this migration
INSERT INTO schema_migrations (version, description)
VALUES ('000_full_schema', 'Complete production schema')
ON CONFLICT (version) DO NOTHING;

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

-- Apply to tables with updated_at
DROP TRIGGER IF EXISTS update_user_api_keys_updated_at ON user_api_keys;
CREATE TRIGGER update_user_api_keys_updated_at
    BEFORE UPDATE ON user_api_keys
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_guild_settings_updated_at ON guild_settings;
CREATE TRIGGER update_guild_settings_updated_at
    BEFORE UPDATE ON guild_settings
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_happy_jump_sessions_updated_at ON happy_jump_sessions;
CREATE TRIGGER update_happy_jump_sessions_updated_at
    BEFORE UPDATE ON happy_jump_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- VERIFICATION QUERIES
-- Run these after migration to verify schema
-- ============================================================================

-- Check all tables exist
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;

-- Check payment type constraints
-- SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname LIKE '%payment%' OR conname LIKE '%cost%';

-- Check indexes
-- SELECT indexname FROM pg_indexes WHERE schemaname = 'public' ORDER BY indexname;
