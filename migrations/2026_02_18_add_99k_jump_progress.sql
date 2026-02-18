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

UPDATE public.jump_99k_sessions
SET host_jump_state = COALESCE(NULLIF(host_jump_state, ''), 'waiting')
WHERE host_jump_state IS NULL OR host_jump_state = '';

UPDATE public.jump_99k_signups
SET jump_state = COALESCE(NULLIF(jump_state, ''), 'waiting')
WHERE jump_state IS NULL OR jump_state = '';
