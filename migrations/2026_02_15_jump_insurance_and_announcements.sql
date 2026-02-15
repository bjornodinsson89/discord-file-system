-- Guild setup config additions for jump announcements
ALTER TABLE public.guild_settings
    ADD COLUMN IF NOT EXISTS jump_announce_channel_id BIGINT,
    ADD COLUMN IF NOT EXISTS jump_ping_role_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 99k insurance request tracking used by post-verification insurance flow
CREATE TABLE IF NOT EXISTS public.jump_99k_insurance_requests (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES public.jump_99k_sessions(id) ON DELETE CASCADE,
    participant_discord_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested','claimed','accepted','declined','denied','completed')),
    channel_id BIGINT,
    message_id BIGINT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_by_discord_id BIGINT,
    claimed_at TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    declined_at TIMESTAMPTZ,
    denied_by_discord_id BIGINT,
    denied_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS jump_99k_insurance_requests_session_user_idx
    ON public.jump_99k_insurance_requests(session_id, participant_discord_id);
