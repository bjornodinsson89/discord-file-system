CREATE TABLE IF NOT EXISTS public.user_torn_identity_cache (
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    torn_user_id BIGINT NOT NULL,
    torn_name TEXT,
    source TEXT NOT NULL,
    is_official_discord_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_verified_at TIMESTAMPTZ,
    CONSTRAINT user_torn_identity_cache_source_check
        CHECK (source IN ('api', 'discord_lookup', 'nickname')),
    CONSTRAINT user_torn_identity_cache_guild_discord_key
        UNIQUE (guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_user_torn_identity_cache_discord_id
    ON public.user_torn_identity_cache (discord_id);

CREATE INDEX IF NOT EXISTS idx_user_torn_identity_cache_torn_user_id
    ON public.user_torn_identity_cache (torn_user_id);

CREATE TABLE IF NOT EXISTS public.xanax_pool_pending_purchases (
    id BIGSERIAL PRIMARY KEY,
    pool_id BIGINT NOT NULL REFERENCES public.xanax_pools(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    buyer_discord_id BIGINT NOT NULL,
    buyer_torn_user_id BIGINT NOT NULL,
    buyer_torn_name TEXT,
    identity_source TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    total_cost_xanax INTEGER NOT NULL,
    reserved_until TIMESTAMPTZ NOT NULL,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT xanax_pool_pending_purchases_identity_source_check
        CHECK (identity_source IN ('api', 'discord_lookup', 'nickname'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_xanax_pool_pending_unique_active
    ON public.xanax_pool_pending_purchases (pool_id, buyer_discord_id)
    WHERE verified_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_xanax_pool_pending_pool_buyer
    ON public.xanax_pool_pending_purchases (pool_id, buyer_discord_id);

CREATE INDEX IF NOT EXISTS idx_xanax_pool_pending_reserved_until
    ON public.xanax_pool_pending_purchases (reserved_until);
