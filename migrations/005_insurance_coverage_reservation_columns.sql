-- Ensure insurance coverage reservation cleanup columns exist.
-- Fixes UndefinedColumnError in cleanup_expired_coverage_reservations.

ALTER TABLE insurance_coverage
    ADD COLUMN IF NOT EXISTS reserved_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_log_timestamp BIGINT;

CREATE INDEX IF NOT EXISTS idx_coverage_reserved_until_pending
    ON insurance_coverage(reserved_until)
    WHERE status = 'pending';
