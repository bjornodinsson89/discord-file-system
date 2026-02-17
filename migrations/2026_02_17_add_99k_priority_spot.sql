BEGIN;

ALTER TABLE public.jump_99k_sessions
  ADD COLUMN IF NOT EXISTS priority_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS priority_increment integer NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS priority_reserved_by_discord_id text,
  ADD COLUMN IF NOT EXISTS priority_reserved_signup_id integer,
  ADD COLUMN IF NOT EXISTS priority_reserved_until timestamptz,
  ADD COLUMN IF NOT EXISTS priority_taken_signup_id integer;

CREATE INDEX IF NOT EXISTS ix_jump_99k_sessions_priority_reserved_until
  ON public.jump_99k_sessions (priority_reserved_until);

ALTER TABLE public.jump_99k_signups
  ADD COLUMN IF NOT EXISTS is_priority boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX IF NOT EXISTS ux_jump_99k_one_priority_signup
  ON public.jump_99k_signups (session_id)
  WHERE is_priority;

CREATE INDEX IF NOT EXISTS ix_jump_99k_signups_roster
  ON public.jump_99k_signups (session_id, is_priority DESC, id ASC);

CREATE OR REPLACE FUNCTION public.jump_99k_reserve_priority(
  p_session_id integer,
  p_buyer_discord_id text,
  p_signup_id integer,
  p_ttl_seconds integer DEFAULT 300
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_updated integer;
BEGIN
  UPDATE public.jump_99k_sessions
  SET
    priority_reserved_by_discord_id = NULL,
    priority_reserved_signup_id = NULL,
    priority_reserved_until = NULL
  WHERE id = p_session_id
    AND priority_reserved_until IS NOT NULL
    AND priority_reserved_until < now();

  UPDATE public.jump_99k_sessions
  SET
    priority_reserved_by_discord_id = p_buyer_discord_id,
    priority_reserved_signup_id = p_signup_id,
    priority_reserved_until = now() + make_interval(secs => p_ttl_seconds)
  WHERE id = p_session_id
    AND priority_enabled = true
    AND priority_taken_signup_id IS NULL
    AND (
      priority_reserved_until IS NULL OR priority_reserved_until < now()
    );

  GET DIAGNOSTICS v_updated = ROW_COUNT;
  RETURN v_updated = 1;
END;
$$;

CREATE OR REPLACE FUNCTION public.jump_99k_finalize_priority(
  p_session_id integer,
  p_buyer_discord_id text,
  p_signup_id integer
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  v_updated integer;
BEGIN
  UPDATE public.jump_99k_sessions
  SET
    priority_taken_signup_id = p_signup_id,
    priority_reserved_by_discord_id = NULL,
    priority_reserved_signup_id = NULL,
    priority_reserved_until = NULL
  WHERE id = p_session_id
    AND priority_taken_signup_id IS NULL
    AND priority_reserved_by_discord_id = p_buyer_discord_id
    AND priority_reserved_signup_id = p_signup_id
    AND priority_reserved_until IS NOT NULL
    AND priority_reserved_until >= now();

  GET DIAGNOSTICS v_updated = ROW_COUNT;

  IF v_updated = 1 THEN
    UPDATE public.jump_99k_signups
    SET is_priority = true
    WHERE id = p_signup_id AND session_id = p_session_id;
    RETURN true;
  END IF;

  RETURN false;
END;
$$;

COMMIT;
