DO $$
DECLARE
    has_discord_id BOOLEAN;
    has_participant_discord_id BOOLEAN;
BEGIN
    IF to_regclass('public.free_raffle_entries') IS NULL THEN
        RETURN;
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'free_raffle_entries'
          AND column_name = 'discord_id'
    ) INTO has_discord_id;

    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'free_raffle_entries'
          AND column_name = 'participant_discord_id'
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
    END IF;

    IF has_discord_id AND NOT has_participant_discord_id THEN
        -- Canonical schema already present; no secondary-column migration needed.
        NULL;
    END IF;

    EXECUTE 'DROP INDEX IF EXISTS public.idx_free_raffle_entries_raffle_user_unique';
    EXECUTE 'DROP INDEX IF EXISTS public.idx_free_raffle_entries_raffle_participant_user_unique';

    IF has_discord_id THEN
        EXECUTE '
            CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_user_unique
            ON public.free_raffle_entries (raffle_id, discord_id)
            WHERE discord_id IS NOT NULL
        ';
    ELSE
        EXECUTE '
            CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_participant_user_unique
            ON public.free_raffle_entries (raffle_id, participant_discord_id)
            WHERE participant_discord_id IS NOT NULL
        ';
    END IF;
END
$$;
