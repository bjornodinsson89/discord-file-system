BEGIN;

CREATE TABLE IF NOT EXISTS public.reward_store_items (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NULL,
    category TEXT NOT NULL,
    token_cost INTEGER NOT NULL,
    stock INTEGER NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    fulfillment_type TEXT NOT NULL,
    discord_role_id BIGINT NULL,
    torn_item_name TEXT NULL,
    torn_item_id BIGINT NULL,
    thumbnail_url TEXT NULL,
    max_per_user INTEGER NULL,
    requires_admin_approval BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT reward_store_items_category_chk CHECK (category IN ('torn_item', 'discord_perk')),
    CONSTRAINT reward_store_items_fulfillment_chk CHECK (fulfillment_type IN ('admin_manual', 'discord_role', 'discord_action'))
);

CREATE TABLE IF NOT EXISTS public.reward_redemptions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    store_item_id BIGINT NOT NULL REFERENCES public.reward_store_items(id),
    token_cost INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL,
    fulfillment_type TEXT NOT NULL,
    notes TEXT NULL,
    fulfilled_by BIGINT NULL,
    fulfilled_at TIMESTAMPTZ NULL,
    admin_message_channel_id BIGINT NULL,
    admin_message_id BIGINT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT reward_redemptions_status_chk CHECK (status IN ('pending', 'fulfilled', 'cancelled', 'refunded')),
    CONSTRAINT reward_redemptions_fulfillment_chk CHECK (fulfillment_type IN ('admin_manual', 'discord_role', 'discord_action'))
);

CREATE TABLE IF NOT EXISTS public.store_guild_settings (
    guild_id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    fulfillment_channel_id BIGINT NULL,
    torn_item_store_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    discord_perk_store_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reward_store_items_guild_active
    ON public.reward_store_items (guild_id, is_active, category);
CREATE INDEX IF NOT EXISTS idx_reward_redemptions_pending
    ON public.reward_redemptions (guild_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reward_redemptions_user
    ON public.reward_redemptions (guild_id, user_id, created_at DESC);

COMMIT;
