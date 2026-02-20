BEGIN;

CREATE INDEX IF NOT EXISTS idx_jump_99k_sessions_status
    ON public.jump_99k_sessions (status);

CREATE INDEX IF NOT EXISTS idx_jump_99k_sessions_guild_id
    ON public.jump_99k_sessions (guild_id);

CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_session_id
    ON public.jump_99k_signups (session_id);

CREATE INDEX IF NOT EXISTS idx_free_raffles_status
    ON public.free_raffles (status);

COMMIT;
