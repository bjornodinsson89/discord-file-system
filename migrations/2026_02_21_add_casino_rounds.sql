CREATE TABLE IF NOT EXISTS public.casino_game_rounds (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    wallet_id BIGINT NOT NULL REFERENCES public.casino_wallets(id) ON DELETE CASCADE,
    game_key TEXT NOT NULL,
    bet_tokens BIGINT NOT NULL,
    payout_tokens BIGINT NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_casino_game_rounds_guild_wallet_created
    ON public.casino_game_rounds (guild_id, wallet_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_casino_game_rounds_guild_game_created
    ON public.casino_game_rounds (guild_id, game_key, created_at DESC);
