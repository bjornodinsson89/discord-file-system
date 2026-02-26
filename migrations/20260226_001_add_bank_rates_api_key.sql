BEGIN;
ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS bank_rates_api_key_encrypted TEXT;
COMMIT;
