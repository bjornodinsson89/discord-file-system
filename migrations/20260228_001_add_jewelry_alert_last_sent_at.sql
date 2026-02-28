BEGIN;
ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS jewelry_alert_last_sent_at TIMESTAMPTZ;
COMMIT;
