ALTER TABLE jump_99k_signups
ADD COLUMN IF NOT EXISTS reserved_until timestamptz;

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_pending
ON jump_99k_signups (session_id, payment_verified, reserved_until)
WHERE payment_verified = FALSE;
