ALTER TABLE jump_99k_sessions
  ADD COLUMN IF NOT EXISTS private_channel_id BIGINT;

ALTER TABLE jump_99k_sessions
  ADD COLUMN IF NOT EXISTS roster_message_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_jump_99k_sessions_private_channel
  ON jump_99k_sessions (private_channel_id);
