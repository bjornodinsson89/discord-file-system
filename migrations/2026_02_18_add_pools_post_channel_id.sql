ALTER TABLE public.guild_settings
ADD COLUMN IF NOT EXISTS pools_post_channel_id bigint;
