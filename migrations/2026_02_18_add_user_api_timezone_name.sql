ALTER TABLE public.user_api_keys
ADD COLUMN IF NOT EXISTS timezone_name TEXT NULL;
