ALTER TABLE xanax_pools
ADD COLUMN IF NOT EXISTS unlimited_tickets boolean NOT NULL DEFAULT false;

ALTER TABLE xanax_pools
ADD COLUMN IF NOT EXISTS end_draw_at timestamptz;

ALTER TABLE xanax_pools
ALTER COLUMN tickets_total DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_xanax_pools_status_end_draw_at
ON xanax_pools (status, end_draw_at);
