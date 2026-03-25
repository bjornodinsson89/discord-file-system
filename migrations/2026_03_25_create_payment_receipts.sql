BEGIN;

CREATE TABLE IF NOT EXISTS public.payment_receipts (
    id BIGSERIAL PRIMARY KEY,
    feature_type TEXT NOT NULL,
    feature_ref_id TEXT NOT NULL,
    payer_discord_id BIGINT NULL,
    payer_torn_user_id BIGINT NULL,
    amount BIGINT NOT NULL,
    currency TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    receipt_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL,
    verified_at TIMESTAMPTZ NULL,
    verified_by_discord_id BIGINT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT payment_receipts_status_check CHECK (status IN ('pending', 'verified', 'rejected'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_receipts_receipt_hash
    ON public.payment_receipts (receipt_hash);

CREATE INDEX IF NOT EXISTS idx_payment_receipts_feature_ref
    ON public.payment_receipts (feature_type, feature_ref_id);

CREATE INDEX IF NOT EXISTS idx_payment_receipts_payer_discord_id
    ON public.payment_receipts (payer_discord_id);

CREATE INDEX IF NOT EXISTS idx_payment_receipts_status
    ON public.payment_receipts (status);

COMMIT;
