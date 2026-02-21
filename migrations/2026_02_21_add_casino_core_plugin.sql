BEGIN;

ALTER TABLE public.guild_settings
    ADD COLUMN IF NOT EXISTS casino_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS casino_house JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS casino_games JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.casino_wallets (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    torn_user_id BIGINT NOT NULL DEFAULT 0,
    torn_name TEXT NULL,
    balance_tokens BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, discord_id)
);

CREATE TABLE IF NOT EXISTS public.casino_ledger (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    wallet_id BIGINT NOT NULL REFERENCES public.casino_wallets(id) ON DELETE CASCADE,
    entry_type TEXT NOT NULL,
    amount_tokens BIGINT NOT NULL,
    balance_after BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL,
    ref_type TEXT NULL,
    ref_id BIGINT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_casino_ledger_guild_created ON public.casino_ledger (guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_casino_ledger_guild_wallet_created ON public.casino_ledger (guild_id, wallet_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.casino_deposits (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    wallet_id BIGINT NOT NULL REFERENCES public.casino_wallets(id) ON DELETE CASCADE,
    torn_log_id TEXT NOT NULL,
    torn_log_ts BIGINT NOT NULL,
    qty_xanax BIGINT NOT NULL,
    raw_log JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, torn_log_id)
);

CREATE TABLE IF NOT EXISTS public.casino_cashouts (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    wallet_id BIGINT NOT NULL REFERENCES public.casino_wallets(id) ON DELETE CASCADE,
    qty_tokens BIGINT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    verified_at TIMESTAMPTZ NULL,
    verified_by_discord_id BIGINT NULL,
    payout_torn_log_id TEXT NULL,
    payout_raw_log JSONB NOT NULL DEFAULT '{}'::jsonb,
    payouts_channel_message_id BIGINT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_casino_cashouts_guild_payout_log_unique
    ON public.casino_cashouts(guild_id, payout_torn_log_id)
    WHERE payout_torn_log_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_casino_cashouts_guild_status_requested
    ON public.casino_cashouts(guild_id, status, requested_at DESC);

CREATE TABLE IF NOT EXISTS public.casino_house_ledger (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    entry_type TEXT NOT NULL,
    amount_tokens BIGINT NOT NULL,
    total_after BIGINT NOT NULL,
    ref_type TEXT NULL,
    ref_id BIGINT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_casino_house_ledger_guild_created ON public.casino_house_ledger (guild_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.casino_pools (
    guild_id BIGINT NOT NULL,
    pool_key TEXT NOT NULL,
    tokens BIGINT NOT NULL DEFAULT 0,
    millis BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, pool_key)
);

CREATE TABLE IF NOT EXISTS public.casino_cooldowns (
    guild_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    game_key TEXT NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guild_id, discord_id, game_key)
);

COMMIT;
