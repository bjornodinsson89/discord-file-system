-- Normalize 99k signup statuses used by application code.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND t.relname = 'jump_99k_signups'
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE public.jump_99k_signups DROP CONSTRAINT IF EXISTS %I', r.conname);
    END LOOP;
END $$;

-- Forward-safe normalization before re-adding status checks that disallow legacy values.
UPDATE public.jump_99k_signups
SET status = 'paid'
WHERE status IN ('signed_up', 'confirmed');

ALTER TABLE public.jump_99k_signups
    ADD CONSTRAINT jump_99k_signups_status_check
    CHECK (status IN ('reserved', 'paid', 'cancelled', 'expired', 'completed', 'not_completed'));

CREATE TABLE IF NOT EXISTS public.jump_99k_cleanup_tasks (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    session_id BIGINT NOT NULL,
    task_type TEXT NOT NULL CHECK (task_type IN ('delete_message', 'delete_channel')),
    channel_id BIGINT,
    message_id BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT jump_99k_cleanup_tasks_unique_task UNIQUE (session_id, task_type, channel_id, message_id)
);

CREATE INDEX IF NOT EXISTS ix_jump_99k_cleanup_tasks_due
    ON public.jump_99k_cleanup_tasks (next_retry_at, attempts);
