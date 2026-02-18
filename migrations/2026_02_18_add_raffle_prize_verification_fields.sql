ALTER TABLE public.raffles
  ADD COLUMN IF NOT EXISTS prize_verified_at timestamptz,
  ADD COLUMN IF NOT EXISTS prize_verified_by_discord_id text,
  ADD COLUMN IF NOT EXISTS prize_verification_status text,
  ADD COLUMN IF NOT EXISTS prize_verification_log_id text,
  ADD COLUMN IF NOT EXISTS prize_verification_checked_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_raffles_prize_verification_status
  ON public.raffles (prize_verification_status);
