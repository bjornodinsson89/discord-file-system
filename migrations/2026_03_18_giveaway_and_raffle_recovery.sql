BEGIN;

ALTER TABLE public.free_raffles
    ADD COLUMN IF NOT EXISTS button_join_enabled BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE public.raffles
    ADD COLUMN IF NOT EXISTS superseded_by_raffle_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS recreated_from_raffle_id BIGINT NULL;

ALTER TABLE public.raffle_entries
    ADD COLUMN IF NOT EXISTS recreated_from_entry_id BIGINT NULL;

CREATE INDEX IF NOT EXISTS idx_raffles_superseded_by_raffle_id
    ON public.raffles (superseded_by_raffle_id)
    WHERE superseded_by_raffle_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raffles_recreated_from_raffle_id
    ON public.raffles (recreated_from_raffle_id)
    WHERE recreated_from_raffle_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raffle_entries_recreated_from_entry_id
    ON public.raffle_entries (recreated_from_entry_id)
    WHERE recreated_from_entry_id IS NOT NULL;

COMMIT;
