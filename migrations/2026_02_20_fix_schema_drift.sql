DO $$
BEGIN
    IF to_regclass('public.jump_99k_insurance_requests') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'jump_99k_insurance_requests'
              AND column_name = 'discord_id'
        ) AND NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'jump_99k_insurance_requests'
              AND column_name = 'participant_discord_id'
        ) THEN
            EXECUTE 'ALTER TABLE public.jump_99k_insurance_requests RENAME COLUMN discord_id TO participant_discord_id';
        END IF;

        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'jump_99k_insurance_requests'
              AND column_name = 'participant_discord_id'
        ) THEN
            EXECUTE 'ALTER TABLE public.jump_99k_insurance_requests ADD COLUMN participant_discord_id BIGINT';
        END IF;

        IF to_regclass('public.jump_99k_signups') IS NOT NULL THEN
            EXECUTE '
                UPDATE public.jump_99k_insurance_requests AS req
                SET participant_discord_id = COALESCE(req.participant_discord_id, signup.discord_id)
                FROM public.jump_99k_signups AS signup
                WHERE req.session_id = signup.session_id
                  AND req.participant_discord_id IS NULL
            ';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'jump_99k_insurance_requests'
              AND column_name = 'participant_discord_id'
        ) THEN
            EXECUTE '
                DELETE FROM public.jump_99k_insurance_requests AS req
                WHERE req.participant_discord_id IS NULL
            ';
            EXECUTE 'ALTER TABLE public.jump_99k_insurance_requests ALTER COLUMN participant_discord_id SET NOT NULL';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'jump_99k_insurance_requests'
              AND indexname = 'jump_99k_insurance_requests_session_user_idx'
        ) THEN
            EXECUTE 'DROP INDEX IF EXISTS public.jump_99k_insurance_requests_session_user_idx';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'jump_99k_insurance_requests'
              AND indexname = 'jump_99k_insurance_requests_session_discord_idx'
        ) THEN
            EXECUTE 'DROP INDEX IF EXISTS public.jump_99k_insurance_requests_session_discord_idx';
        END IF;

        EXECUTE '
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jump_99k_insurance_requests_session_participant
                ON public.jump_99k_insurance_requests(session_id, participant_discord_id)
        ';
    END IF;
END
$$;

DO $$
DECLARE
    has_created_at BOOLEAN;
BEGIN
    IF to_regclass('public.free_raffle_winners') IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'free_raffle_winners'
              AND column_name = 'created_at'
        ) THEN
            EXECUTE 'ALTER TABLE public.free_raffle_winners ADD COLUMN created_at TIMESTAMPTZ';
        END IF;

        EXECUTE 'ALTER TABLE public.free_raffle_winners ALTER COLUMN created_at SET DEFAULT NOW()';
        EXECUTE 'UPDATE public.free_raffle_winners SET created_at = NOW() WHERE created_at IS NULL';
        EXECUTE 'ALTER TABLE public.free_raffle_winners ALTER COLUMN created_at SET NOT NULL';

        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'free_raffle_winners'
              AND column_name = 'created_at'
        ) INTO has_created_at;

        IF has_created_at THEN
            EXECUTE '
                WITH ranked AS (
                    SELECT ctid,
                           ROW_NUMBER() OVER (PARTITION BY raffle_id ORDER BY created_at DESC NULLS LAST, ctid DESC) AS rn
                    FROM public.free_raffle_winners
                )
                DELETE FROM public.free_raffle_winners w
                USING ranked r
                WHERE w.ctid = r.ctid
                  AND r.rn > 1
            ';
        ELSE
            EXECUTE '
                WITH ranked AS (
                    SELECT ctid,
                           ROW_NUMBER() OVER (PARTITION BY raffle_id ORDER BY ctid DESC) AS rn
                    FROM public.free_raffle_winners
                )
                DELETE FROM public.free_raffle_winners w
                USING ranked r
                WHERE w.ctid = r.ctid
                  AND r.rn > 1
            ';
        END IF;

        EXECUTE '
            CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_winners_raffle_id
                ON public.free_raffle_winners(raffle_id)
        ';
    END IF;
END
$$;
