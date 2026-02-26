BEGIN;
ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS jewelry_alert_channel_id BIGINT,
  ADD COLUMN IF NOT EXISTS jewelry_alert_role_ids JSONB,
  ADD COLUMN IF NOT EXISTS jewelry_alert_active_message_id BIGINT,
  ADD COLUMN IF NOT EXISTS jewelry_alert_last_clear BOOLEAN DEFAULT FALSE;
COMMIT;
