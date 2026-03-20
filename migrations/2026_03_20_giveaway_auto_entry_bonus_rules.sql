BEGIN;

ALTER TABLE public.free_raffles
    ADD COLUMN IF NOT EXISTS messages_per_entry INTEGER NOT NULL DEFAULT 15;

ALTER TABLE public.free_raffles
    ADD COLUMN IF NOT EXISTS auto_entry_max_per_user INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS public.free_raffle_role_bonuses (
    raffle_id BIGINT NOT NULL REFERENCES public.free_raffles(id) ON DELETE CASCADE,
    role_id BIGINT NOT NULL,
    bonus_entries_per_qualification INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (raffle_id, role_id)
);

COMMIT;
