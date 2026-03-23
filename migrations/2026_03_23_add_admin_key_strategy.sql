ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS admin_key_strategy TEXT NOT NULL DEFAULT 'pool',
  ADD COLUMN IF NOT EXISTS admin_key_single_discord_id BIGINT;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'guild_settings_admin_key_strategy_check'
      AND conrelid = 'public.guild_settings'::regclass
  ) THEN
    ALTER TABLE public.guild_settings
      ADD CONSTRAINT guild_settings_admin_key_strategy_check
      CHECK (admin_key_strategy IN ('pool', 'single'));
  END IF;
END $$;
