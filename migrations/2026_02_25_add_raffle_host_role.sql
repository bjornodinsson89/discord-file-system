ALTER TABLE guild_settings
    ADD COLUMN IF NOT EXISTS raffle_host_role_id BIGINT NULL;

UPDATE guild_settings
SET raffle_host_role_id = paid_raffle_admin_role_id
WHERE raffle_host_role_id IS NULL
  AND paid_raffle_admin_role_id IS NOT NULL;
