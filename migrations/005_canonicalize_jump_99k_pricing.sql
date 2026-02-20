ALTER TABLE public.jump_99k_sessions
    ADD COLUMN IF NOT EXISTS price_item TEXT,
    ADD COLUMN IF NOT EXISTS price_amount INT,
    ADD COLUMN IF NOT EXISTS scheduled_start_text TEXT;

UPDATE public.jump_99k_sessions
SET price_item = COALESCE(price_item, 'xanax'),
    price_amount = COALESCE(price_amount, 1);

ALTER TABLE public.jump_99k_sessions
    ALTER COLUMN price_item SET NOT NULL,
    ALTER COLUMN price_amount SET NOT NULL;

ALTER TABLE public.jump_99k_sessions
    DROP COLUMN IF EXISTS price_quantity;

-- legacy cleanup: DROP COLUMN price_quantity
