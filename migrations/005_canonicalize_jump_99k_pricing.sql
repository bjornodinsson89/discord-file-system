BEGIN;

ALTER TABLE jump_99k_sessions
  ADD COLUMN IF NOT EXISTS price_item TEXT;

ALTER TABLE jump_99k_sessions
  ADD COLUMN IF NOT EXISTS price_amount INT;

ALTER TABLE jump_99k_sessions
  ADD COLUMN IF NOT EXISTS scheduled_start_text TEXT;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'jump_99k_sessions' AND column_name = 'price_quantity'
  ) THEN
    EXECUTE 'UPDATE jump_99k_sessions
             SET price_amount = COALESCE(price_amount, price_quantity)
             WHERE price_amount IS NULL';
  END IF;
END $$;

UPDATE jump_99k_sessions
SET price_item = COALESCE(price_item, 'xanax')
WHERE price_item IS NULL;

UPDATE jump_99k_sessions
SET price_amount = COALESCE(price_amount, 1)
WHERE price_amount IS NULL;

ALTER TABLE jump_99k_sessions
  ALTER COLUMN price_item SET NOT NULL;

ALTER TABLE jump_99k_sessions
  ALTER COLUMN price_amount SET NOT NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'jump_99k_sessions' AND column_name = 'price_quantity'
  ) THEN
    EXECUTE 'ALTER TABLE jump_99k_sessions DROP COLUMN price_quantity';
  END IF;
END $$;

COMMIT;
