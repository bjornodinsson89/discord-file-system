-- Migration 002: vNext Schema Updates
-- Updates schema according to Discord bot requirements

-- ============================================================================
-- HAPPY JUMP SESSIONS: Update xanax_count to xanax_stack with new validation
-- ============================================================================
ALTER TABLE happy_jump_sessions 
ADD COLUMN IF NOT EXISTS xanax_stack VARCHAR(20);

-- Migrate existing data
UPDATE happy_jump_sessions 
SET xanax_stack = CASE 
    WHEN xanax_count = 1 THEN '1 xanax'
    WHEN xanax_count = 2 THEN '2 xanax'
    WHEN xanax_count = 3 THEN '3 xanax'
    WHEN xanax_count >= 100 THEN 'full_stack'
    ELSE '1 xanax'
END
WHERE xanax_stack IS NULL;

-- Add constraint for valid values
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_xanax_stack'
    ) THEN
        ALTER TABLE happy_jump_sessions
        ADD CONSTRAINT chk_xanax_stack
        CHECK (xanax_stack IN ('1_xanax', '2_xanax', '3_xanax', 'full_stack'));
    END IF;
END $$;

-- ============================================================================
-- INSURANCE POLICIES: Restructure for new form fields
-- ============================================================================

-- Add new columns for updated insurance model
ALTER TABLE insurance_policies 
ADD COLUMN IF NOT EXISTS cost_type VARCHAR(20);

ALTER TABLE insurance_policies 
ADD COLUMN IF NOT EXISTS cost_amount INTEGER;

ALTER TABLE insurance_policies 
ADD COLUMN IF NOT EXISTS coverage_type VARCHAR(50);

ALTER TABLE insurance_policies 
ADD COLUMN IF NOT EXISTS payout_description TEXT;

-- Migrate existing data
UPDATE insurance_policies 
SET 
    cost_type = 'xanax',
    cost_amount = premium_per_xanax,
    coverage_type = 'xanax_stack',
    payout_description = CONCAT(payout_per_xanax::TEXT, ' per xanax lost')
WHERE cost_type IS NULL;

-- Add constraint for valid coverage types
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_coverage_type'
    ) THEN
        ALTER TABLE insurance_policies
        ADD CONSTRAINT chk_coverage_type
        CHECK (coverage_type IN ('xanax_stack', 'ecstasy_after_stack', 'all_drugs'));
    END IF;
END $$;

-- Add constraint for valid cost types
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_cost_type'
    ) THEN
        ALTER TABLE insurance_policies
        ADD CONSTRAINT chk_cost_type
        CHECK (cost_type IN ('xanax', 'erotic_dvd'));
    END IF;
END $$;

-- ============================================================================
-- RAFFLES: Rename max_tickets to tickets_available
-- ============================================================================
ALTER TABLE raffles 
ADD COLUMN IF NOT EXISTS tickets_available INTEGER;

-- Migrate existing data
UPDATE raffles 
SET tickets_available = max_tickets 
WHERE tickets_available IS NULL;

-- Add not null constraint
ALTER TABLE raffles 
ALTER COLUMN tickets_available SET NOT NULL;

-- ============================================================================
-- ADD METADATA COLUMNS FOR DASHBOARD ACTIONS
-- ============================================================================




-- Add provider approval workflow fields
ALTER TABLE insurance_providers 
ADD COLUMN IF NOT EXISTS approval_status VARCHAR(20) DEFAULT 'pending';

ALTER TABLE insurance_providers 
ADD COLUMN IF NOT EXISTS approved_by BIGINT;

ALTER TABLE insurance_providers 
ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_approval_status'
    ) THEN
        ALTER TABLE insurance_providers
        ADD CONSTRAINT chk_approval_status
        CHECK (approval_status IN ('pending', 'approved', 'rejected', 'disabled'));
    END IF;
END $$;

-- ============================================================================
-- INDEXES FOR DASHBOARD QUERIES
-- ============================================================================

-- Index for session queries
CREATE INDEX IF NOT EXISTS idx_sessions_created_at 
ON happy_jump_sessions(created_at DESC);

-- Index for raffle queries
CREATE INDEX IF NOT EXISTS idx_raffles_created_at 
ON raffles(created_at DESC);

-- Index for insurance queries
CREATE INDEX IF NOT EXISTS idx_policies_provider_active 
ON insurance_policies(provider_id, active);

-- Index for provider approval workflow
CREATE INDEX IF NOT EXISTS idx_providers_approval_status 
ON insurance_providers(approval_status);

-- Index for audit log source queries
CREATE INDEX IF NOT EXISTS idx_audit_created_desc 
ON audit_log(created_at DESC);
