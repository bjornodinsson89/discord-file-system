CREATE TABLE IF NOT EXISTS payment_receipts (
    id BIGSERIAL PRIMARY KEY,
    feature_type TEXT NOT NULL,
    feature_ref_id BIGINT NOT NULL,
    payer_discord_id BIGINT,
    payer_torn_id BIGINT,
    payee_discord_id BIGINT,
    payee_torn_id BIGINT,
    amount BIGINT NOT NULL,
    currency_type TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    verified BOOLEAN NOT NULL DEFAULT FALSE,
    verified_at TIMESTAMPTZ,
    verifier_discord_id BIGINT,
    verifier_torn_id BIGINT,
    verification_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_receipts_feature
    ON payment_receipts (feature_type, feature_ref_id);

CREATE TABLE IF NOT EXISTS jump_99k_session_totals (
    summary_key TEXT PRIMARY KEY,
    completed_count INTEGER NOT NULL DEFAULT 0,
    not_completed_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
