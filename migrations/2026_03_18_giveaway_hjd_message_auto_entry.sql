BEGIN;

ALTER TABLE public.free_raffles
    ADD COLUMN IF NOT EXISTS auto_entry_max_per_user INTEGER NOT NULL DEFAULT 1;

ALTER TABLE public.free_raffle_entries
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE public.free_raffle_entries
    ADD COLUMN IF NOT EXISTS entry_source TEXT NOT NULL DEFAULT 'button',
    ADD COLUMN IF NOT EXISTS entry_weight INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle_user_unique
    ON public.free_raffle_entries (raffle_id, discord_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_dedupe_key
    ON public.free_raffle_entries (raffle_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.giveaway_auto_progress (
    raffle_id BIGINT NOT NULL REFERENCES public.free_raffles(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    qualifying_message_count INTEGER NOT NULL DEFAULT 0,
    auto_entries_granted INTEGER NOT NULL DEFAULT 0,
    last_award_dedupe_key TEXT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (raffle_id, user_id)
);

ALTER TABLE public.engagement_profiles
    ADD COLUMN IF NOT EXISTS hjd_balance BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hjd_lifetime_earned BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hjd_lifetime_spent BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS public.happy_jump_dollar_transactions (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    transaction_type TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance_after BIGINT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reversed_at TIMESTAMPTZ NULL,
    reversal_reason TEXT NULL,
    UNIQUE (guild_id, dedupe_key)
);

COMMIT;
