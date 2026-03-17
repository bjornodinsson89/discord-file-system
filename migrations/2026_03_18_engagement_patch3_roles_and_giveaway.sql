BEGIN;

CREATE TABLE IF NOT EXISTS public.engagement_role_rewards (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    level_required INTEGER NOT NULL,
    role_id BIGINT NOT NULL,
    remove_lower_tiers BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.engagement_prize_roles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    milestone_type TEXT NOT NULL,
    milestone_value INTEGER NOT NULL,
    role_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.free_raffles
    ADD COLUMN IF NOT EXISTS auto_entry_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS weighted_odds_enabled BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.free_raffle_entries
    ADD COLUMN IF NOT EXISTS entry_source TEXT NOT NULL DEFAULT 'button',
    ADD COLUMN IF NOT EXISTS entry_weight INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS dedupe_key TEXT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_free_raffle_entries_dedupe_key
    ON public.free_raffle_entries (raffle_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

COMMIT;
