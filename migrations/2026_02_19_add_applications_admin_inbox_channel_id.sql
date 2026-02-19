ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS applications_admin_inbox_channel_id BIGINT;
