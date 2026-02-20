ALTER TABLE IF EXISTS public.user_api_keys
  ADD COLUMN IF NOT EXISTS torn_name text NULL;
