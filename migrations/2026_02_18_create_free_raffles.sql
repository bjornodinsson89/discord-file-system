CREATE TABLE IF NOT EXISTS free_raffles (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    host_discord_id BIGINT NOT NULL,
    prize_text TEXT NOT NULL,
    note_text TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    CONSTRAINT free_raffles_status_check CHECK (status IN ('active', 'ended', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_free_raffles_guild_status
    ON free_raffles (guild_id, status);

CREATE INDEX IF NOT EXISTS idx_free_raffles_channel
    ON free_raffles (channel_id);

CREATE TABLE IF NOT EXISTS free_raffle_entries (
    raffle_id BIGINT NOT NULL REFERENCES free_raffles(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (raffle_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_free_raffle_entries_raffle
    ON free_raffle_entries (raffle_id);

CREATE TABLE IF NOT EXISTS free_raffle_winners (
    raffle_id BIGINT PRIMARY KEY REFERENCES free_raffles(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
