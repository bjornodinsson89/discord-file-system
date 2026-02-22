CREATE TABLE IF NOT EXISTS slot_assets (
  combo TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  message_id BIGINT NOT NULL,
  frames INT NOT NULL DEFAULT 40,
  duration_ms INT NOT NULL DEFAULT 110,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_slot_assets_created_at ON slot_assets(created_at);
