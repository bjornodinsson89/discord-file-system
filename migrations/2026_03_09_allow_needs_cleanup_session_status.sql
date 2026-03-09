DO $$
DECLARE
    c record;
BEGIN
    IF to_regclass('public.jump_99k_sessions') IS NULL THEN
        RETURN;
    END IF;

    FOR c IN
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'public.jump_99k_sessions'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE public.jump_99k_sessions DROP CONSTRAINT IF EXISTS %I', c.conname);
    END LOOP;

    ALTER TABLE public.jump_99k_sessions
        ADD CONSTRAINT jump_99k_sessions_status_check
        CHECK (status IN ('open', 'closed', 'cancelled', 'expired', 'completed', 'needs_cleanup'));
END
$$;
