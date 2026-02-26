BEGIN;
ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS jewelry_alert_last_is_open BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS jewelry_alert_last_announcement_message_id BIGINT,
  ADD COLUMN IF NOT EXISTS jewelry_alert_last_announcement_channel_id BIGINT;
COMMIT;
