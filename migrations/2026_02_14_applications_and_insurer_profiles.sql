CREATE TABLE IF NOT EXISTS public.applications (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    app_type TEXT NOT NULL CHECK (app_type IN ('99k_host','insurer')),
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress','submitted','approved','denied','expired')),
    thread_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    current_question INT NOT NULL DEFAULT 0,
    answers JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_by BIGINT NULL,
    reviewed_at TIMESTAMPTZ NULL,
    denial_reason TEXT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS applications_open_unique
    ON public.applications (guild_id, user_id, app_type)
    WHERE status IN ('in_progress','submitted');

CREATE INDEX IF NOT EXISTS applications_guild_status_idx
    ON public.applications (guild_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.insurer_profiles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    display_name TEXT NOT NULL,
    coverage_summary TEXT NOT NULL,
    pricing_text TEXT NOT NULL,
    rules_exclusions TEXT NOT NULL,
    response_time_text TEXT NULL,
    contact_notes TEXT NULL,
    image_url TEXT NULL,
    activation_delay_minutes INT NOT NULL DEFAULT 0,
    coverage_duration_minutes INT NOT NULL DEFAULT 120,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS public.insurer_profile_wizards (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    step INT NOT NULL DEFAULT 0,
    draft JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);
