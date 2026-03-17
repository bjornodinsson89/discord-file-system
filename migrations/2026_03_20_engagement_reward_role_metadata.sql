BEGIN;

ALTER TABLE public.engagement_role_rewards
    ADD COLUMN IF NOT EXISTS role_name TEXT,
    ADD COLUMN IF NOT EXISTS role_color TEXT,
    ADD COLUMN IF NOT EXISTS auto_created BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE public.engagement_prize_roles
    ADD COLUMN IF NOT EXISTS role_name TEXT,
    ADD COLUMN IF NOT EXISTS role_color TEXT,
    ADD COLUMN IF NOT EXISTS auto_created BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE public.engagement_role_rewards
SET role_name = COALESCE(role_name, CONCAT('Level ', level_required, ' Reward')),
    role_color = COALESCE(role_color, '000000');

UPDATE public.engagement_prize_roles
SET role_name = COALESCE(role_name, CONCAT(milestone_type, ':', milestone_value)),
    role_color = COALESCE(role_color, '000000');

COMMIT;
