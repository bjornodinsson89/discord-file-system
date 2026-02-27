CREATE TABLE IF NOT EXISTS public.casino_slots_server_seeds (
  guild_id BIGINT PRIMARY KEY,
  server_seed TEXT NOT NULL,
  server_seed_hash TEXT NOT NULL,
  previous_server_seed TEXT NULL,
  previous_server_seed_hash TEXT NULL,
  rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  previous_rotated_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS public.casino_slots_player_state (
  guild_id BIGINT NOT NULL,
  discord_id BIGINT NOT NULL,
  client_seed TEXT NOT NULL,
  nonce BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (guild_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_casino_slots_player_state_updated_at
  ON public.casino_slots_player_state (updated_at);
