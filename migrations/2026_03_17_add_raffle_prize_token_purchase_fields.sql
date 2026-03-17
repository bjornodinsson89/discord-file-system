ALTER TABLE public.raffles
ADD COLUMN IF NOT EXISTS allow_prize_token_purchase BOOLEAN NOT NULL DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS prize_token_cost_per_ticket INTEGER;

ALTER TABLE public.raffles
DROP CONSTRAINT IF EXISTS raffles_prize_token_cost_check;

ALTER TABLE public.raffles
ADD CONSTRAINT raffles_prize_token_cost_check CHECK (
  allow_prize_token_purchase = FALSE
  OR (prize_token_cost_per_ticket IS NOT NULL AND prize_token_cost_per_ticket > 0)
);
