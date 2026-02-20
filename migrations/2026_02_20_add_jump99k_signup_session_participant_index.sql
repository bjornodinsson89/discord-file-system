CREATE INDEX IF NOT EXISTS idx_jump_99k_signups_session_participant
    ON public.jump_99k_signups (session_id, participant_discord_id);
