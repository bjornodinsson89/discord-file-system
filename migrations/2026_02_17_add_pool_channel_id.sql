ALTER TABLE public.guild_settings
ADD COLUMN IF NOT EXISTS pool_channel_id bigint;
