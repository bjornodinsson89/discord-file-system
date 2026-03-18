BEGIN;

ALTER TABLE public.torn_items
    ADD COLUMN IF NOT EXISTS description TEXT NULL;

COMMIT;
