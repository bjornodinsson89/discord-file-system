BEGIN;

ALTER TABLE public.free_raffle_winners
    ADD COLUMN IF NOT EXISTS created_at timestamptz;

UPDATE public.free_raffle_winners
SET created_at = COALESCE(created_at, NOW())
WHERE created_at IS NULL;

ALTER TABLE public.free_raffle_winners
    ALTER COLUMN created_at SET DEFAULT NOW();

ALTER TABLE public.free_raffle_winners
    ALTER COLUMN created_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_free_raffle_winners_created_at_desc
    ON public.free_raffle_winners (created_at DESC);

COMMIT;
