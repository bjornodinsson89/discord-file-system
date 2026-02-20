BEGIN;

ALTER TABLE public.jump_99k_sessions
    ADD COLUMN IF NOT EXISTS host_controls_channel_id bigint NULL;

ALTER TABLE public.jump_99k_sessions
    ADD COLUMN IF NOT EXISTS host_controls_message_id bigint NULL;

CREATE INDEX IF NOT EXISTS idx_jump_99k_sessions_host_controls_message_id
    ON public.jump_99k_sessions (host_controls_message_id);

COMMIT;
