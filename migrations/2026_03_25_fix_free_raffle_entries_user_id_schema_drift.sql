DO $$
DECLARE
    has_discord_id BOOLEAN;
    has_participant_discord_id BOOLEAN;
BEGIN
    IF to_regclass('public.free_raffle_entries') IS NULL THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'free_raffle_entries' AND column_name = 'discord_id'
    ) INTO has_discord_id;

    SELECT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'free_raffle_entries' AND column_name = 'participant_discord_id'
    ) INTO has_participant_discord_id;

    IF has_participant_discord_id AND NOT has_discord_id THEN
        EXECUTE 'ALTER TABLE public.free_raffle_entries ADD COLUMN IF NOT EXISTS discord_id BIGINT';
        has_discord_id := TRUE;
    END IF;

    IF has_discord_id AND has_participant_discord_id THEN
        EXECUTE '
            UPDATE public.free_raffle_entries
            SET discord_id = COALESCE(discord_id, participant_discord_id),
                participant_discord_id = COALESCE(participant_discord_id, discord_id)
            WHERE discord_id IS NULL OR participant_discord_id IS NULL
        ';
    ELSIF has_discord_id THEN
        EXECUTE '
            UPDATE public.free_raffle_entries
            SET discord_id = discord_id
            WHERE discord_id IS NOT NULL
        ';
    END IF;

    EXECUTE 'DROP INDEX IF EXISTS public.idx_free_raffle_entries_raffle_user_unique';
    EXECUTE 'DROP INDEX IF EXISTS public.idx_free_raffle_entries_raffle_participant_user_unique';

    IF has_discord_id THEN
        EXECUTE '
            WITH ranked AS (
                SELECT
                    ctid,
                    raffle_id,
                    discord_id,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY raffle_id, discord_id
                        ORDER BY COALESCE(created_at, NOW()) ASC, ctid ASC
                    ) AS rn,
                    SUM(GREATEST(1, COALESCE(entry_weight, 1))) OVER (
                        PARTITION BY raffle_id, discord_id
                    ) AS total_weight,
                    MAX(NULLIF(BTRIM(entry_source), '''')) OVER (
                        PARTITION BY raffle_id, discord_id
                    ) AS keep_entry_source
                FROM public.free_raffle_entries
                WHERE discord_id IS NOT NULL
            )
            UPDATE public.free_raffle_entries AS e
            SET entry_weight = r.total_weight,
                entry_source = COALESCE(r.keep_entry_source, e.entry_source, ''button''),
                dedupe_key = NULL,
                created_at = COALESCE(r.created_at, e.created_at)
            FROM ranked r
            WHERE e.ctid = r.ctid
              AND r.rn = 1
        ';

        IF has_participant_discord_id THEN
            EXECUTE '
                UPDATE public.free_raffle_entries
                SET participant_discord_id = COALESCE(participant_discord_id, discord_id)
                WHERE discord_id IS NOT NULL
            ';
        END IF;

        EXECUTE '
            WITH ranked AS (
                SELECT
                    ctid,
                    ROW_NUMBER() OVER (
                        PARTITION BY raffle_id, discord_id
                        ORDER BY COALESCE(created_at, NOW()) ASC, ctid ASC
                    ) AS rn
                FROM public.free_raffle_entries
                WHERE discord_id IS NOT NULL
            )
            DELETE FROM public.free_raffle_entries e
            USING ranked r
            WHERE e.ctid = r.ctid
              AND r.rn > 1
        ';

        EXECUTE '
            WITH dupes AS (
                SELECT raffle_id, dedupe_key
                FROM public.free_raffle_entries
                WHERE dedupe_key IS NOT NULL
                GROUP BY raffle_id, dedupe_key
                HAVING COUNT(*) > 1
            )
            UPDATE public.free_raffle_entries e
            SET dedupe_key = NULL
            FROM dupes d
            WHERE e.raffle_id = d.raffle_id
              AND e.dedupe_key = d.dedupe_key
        ';

        EXECUTE '
            CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_user_unique
            ON public.free_raffle_entries (raffle_id, discord_id)
        ';
    END IF;

    EXECUTE 'DROP INDEX IF EXISTS public.idx_free_raffle_entries_dedupe_key';
    EXECUTE '
        CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_dedupe_key
        ON public.free_raffle_entries (raffle_id, dedupe_key)
        WHERE dedupe_key IS NOT NULL
    ';
END
$$;
