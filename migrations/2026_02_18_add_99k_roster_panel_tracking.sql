ALTER TABLE public.jump_99k_sessions
    ADD COLUMN IF NOT EXISTS roster_channel_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS roster_message_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS roster_last_refreshed_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_jump_99k_sessions_roster_message
    ON public.jump_99k_sessions (roster_message_id);
