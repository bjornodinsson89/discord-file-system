BEGIN;

ALTER TABLE public.store_guild_settings
    ADD COLUMN IF NOT EXISTS store_channel_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS storefront_channel_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS store_hub_message_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS store_admin_message_id BIGINT NULL;

ALTER TABLE public.reward_store_items
    ADD COLUMN IF NOT EXISTS storefront_channel_id BIGINT NULL,
    ADD COLUMN IF NOT EXISTS storefront_message_id BIGINT NULL;

COMMIT;
