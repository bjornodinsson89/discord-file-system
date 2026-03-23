CREATE TABLE IF NOT EXISTS public.guild_admin_key_pool_members (
    guild_id BIGINT NOT NULL,
    discord_user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT guild_admin_key_pool_members_pkey PRIMARY KEY (guild_id, discord_user_id)
);

CREATE INDEX IF NOT EXISTS idx_guild_admin_key_pool_members_guild_id
    ON public.guild_admin_key_pool_members (guild_id);
