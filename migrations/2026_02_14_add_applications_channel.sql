ALTER TABLE public.guild_settings
    ADD COLUMN IF NOT EXISTS applications_channel_id bigint;
