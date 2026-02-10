ALTER TABLE insurance_policies
ADD COLUMN IF NOT EXISTS payout_items JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE insurance_claims
ADD COLUMN IF NOT EXISTS payout_items JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE insurance_claims
ADD COLUMN IF NOT EXISTS payout_log_id BIGINT;

ALTER TABLE insurance_claims
ADD COLUMN IF NOT EXISTS payout_log_timestamp BIGINT;

ALTER TABLE insurance_claims
ADD COLUMN IF NOT EXISTS payout_log_evidence TEXT;
