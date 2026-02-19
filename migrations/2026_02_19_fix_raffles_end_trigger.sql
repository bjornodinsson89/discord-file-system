UPDATE raffles
SET end_trigger = 'time'
WHERE end_trigger = 'end_time';

ALTER TABLE raffles
DROP CONSTRAINT IF EXISTS raffles_end_trigger_check;

ALTER TABLE raffles
ADD CONSTRAINT raffles_end_trigger_check
CHECK (end_trigger IN ('time', 'tickets_sold'));

UPDATE raffles
SET end_time = NOW() + INTERVAL '30 days'
WHERE end_trigger = 'time'
  AND end_time IS NULL
  AND status = 'active';
