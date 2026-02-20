BEGIN;

ALTER TABLE public.jump_99k_sessions
    ADD COLUMN IF NOT EXISTS host_jump_state TEXT NOT NULL DEFAULT 'waiting',
    ADD COLUMN IF NOT EXISTS host_jump_started_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS host_jump_ended_at TIMESTAMPTZ NULL;

ALTER TABLE public.jump_99k_signups
    ADD COLUMN IF NOT EXISTS jump_state TEXT NOT NULL DEFAULT 'waiting',
    ADD COLUMN IF NOT EXISTS jump_started_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS jump_ended_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_session_jump_state
    ON public.jump_99k_signups (session_id, jump_state);

UPDATE jump_99k_signups
SET status='reserved'
WHERE status IN ('signed_up','confirmed')
  AND COALESCE(payment_verified,FALSE)=FALSE;

UPDATE jump_99k_signups
SET status='paid',
    reserved_until=NULL
WHERE status='reserved'
  AND COALESCE(payment_verified,FALSE)=TRUE;

COMMIT;
