ALTER TABLE public.jump_99k_signups
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE public.jump_99k_signups
SET created_at = COALESCE(created_at, signed_up_at, NOW())
WHERE created_at IS NULL;

UPDATE public.jump_99k_signups
SET updated_at = COALESCE(updated_at, created_at, signed_up_at, NOW())
WHERE updated_at IS NULL;

ALTER TABLE public.jump_99k_signups
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET NOT NULL;
