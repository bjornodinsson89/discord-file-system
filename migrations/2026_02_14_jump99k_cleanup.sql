ALTER TABLE public.jump_99k_sessions
  ADD COLUMN IF NOT EXISTS cleaned_at timestamptz;
