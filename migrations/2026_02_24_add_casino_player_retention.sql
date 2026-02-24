CREATE TABLE IF NOT EXISTS public.casino_player_retention (
  guild_id BIGINT NOT NULL,
  discord_id BIGINT NOT NULL,
  game TEXT NOT NULL,
  plays INT NOT NULL DEFAULT 0,
  loss_streak INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (guild_id, discord_id, game)
);

CREATE INDEX IF NOT EXISTS idx_casino_player_retention_game
  ON public.casino_player_retention (game);
