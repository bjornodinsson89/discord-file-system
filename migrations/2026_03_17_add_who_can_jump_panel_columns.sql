BEGIN;

ALTER TABLE public.guild_settings
    ADD COLUMN IF NOT EXISTS who_can_jump_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS who_can_jump_message_id BIGINT;

COMMIT;
