ALTER TABLE public.jump_99k_sessions
  ADD COLUMN IF NOT EXISTS start_time timestamptz;
