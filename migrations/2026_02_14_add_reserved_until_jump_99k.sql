-- Required for 99k reservation windows / payment verification flows.
-- Supabase SQL (safe to run multiple times):
-- ALTER TABLE public.jump_99k_signups ADD COLUMN IF NOT EXISTS reserved_until timestamptz;
-- CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_reserved_until ON public.jump_99k_signups (reserved_until);

ALTER TABLE public.jump_99k_signups
ADD COLUMN IF NOT EXISTS reserved_until timestamptz;

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_reserved_until
ON public.jump_99k_signups (reserved_until);

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_pending
ON public.jump_99k_signups (session_id, payment_verified, reserved_until)
WHERE payment_verified = FALSE;
