ALTER TABLE free_raffles
    ADD COLUMN IF NOT EXISTS ends_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS drawn_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS winner_discord_id TEXT;

UPDATE free_raffles
SET ends_at = created_at + INTERVAL '1 day'
WHERE ends_at IS NULL;

ALTER TABLE free_raffles
    ALTER COLUMN ends_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_free_raffles_status_ends_at
    ON free_raffles (status, ends_at);
