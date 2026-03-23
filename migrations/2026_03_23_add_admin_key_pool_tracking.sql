ALTER TABLE IF EXISTS public.user_api_keys
    ADD COLUMN IF NOT EXISTS invalid_key_fail_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS invalid_key_last_failed_at TIMESTAMPTZ NULL;

CREATE INDEX IF NOT EXISTS idx_user_api_keys_invalid_key_fail_count_active
    ON public.user_api_keys (invalid_key_fail_count)
    WHERE invalid_key_fail_count > 0;
