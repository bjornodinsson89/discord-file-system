BEGIN;

ALTER TABLE public.engagement_guild_settings
    ADD COLUMN IF NOT EXISTS level_up_coin_reward INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS level_up_hjd_reward INTEGER NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS paid_raffle_purchase_coin_reward INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS paid_raffle_purchase_hjd_reward INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS jump_purchase_coin_reward INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS jump_purchase_hjd_reward INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS jump_completion_coin_reward INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS jump_completion_hjd_reward INTEGER NOT NULL DEFAULT 0;

COMMIT;
