CREATE TABLE IF NOT EXISTS casino_slots_accounting (
  id BIGSERIAL PRIMARY KEY,
  guild_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  actor_discord_id BIGINT NOT NULL,
  round_id BIGINT NULL,
  bet BIGINT NOT NULL DEFAULT 0,
  payout BIGINT NOT NULL DEFAULT 0,
  win_type TEXT NOT NULL DEFAULT 'loss',
  jackpot_contrib BIGINT NOT NULL DEFAULT 0,
  jackpot_payout BIGINT NOT NULL DEFAULT 0,
  jackpot_admin_add BIGINT NOT NULL DEFAULT 0,
  jackpot_overflow_to_house BIGINT NOT NULL DEFAULT 0,
  jackpot_pool_before BIGINT NOT NULL DEFAULT 0,
  jackpot_pool_after BIGINT NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_slots_acct_guild_time
ON casino_slots_accounting (guild_id, created_at DESC);
