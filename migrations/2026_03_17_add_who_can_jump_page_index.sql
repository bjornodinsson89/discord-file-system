BEGIN;

ALTER TABLE public.guild_settings
    ADD COLUMN IF NOT EXISTS who_can_jump_page_index INTEGER NOT NULL DEFAULT 0;

COMMIT;
