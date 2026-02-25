CREATE TABLE IF NOT EXISTS api_audit_log (
  id BIGSERIAL PRIMARY KEY,
  discord_id BIGINT NOT NULL,
  torn_id INT NULL,
  context TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  selections TEXT NULL,
  query_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  status TEXT NOT NULL CHECK (status IN ('ok','error')),
  http_status INT NULL,
  duration_ms INT NULL,
  error_code TEXT NULL,
  error_message TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS api_audit_log_discord_created_idx
  ON api_audit_log (discord_id, created_at DESC);

ALTER TABLE public.guild_settings
ADD COLUMN IF NOT EXISTS paid_raffle_admin_role_id BIGINT NULL;
