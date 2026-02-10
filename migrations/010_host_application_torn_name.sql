ALTER TABLE host_applications
    ADD COLUMN IF NOT EXISTS torn_name VARCHAR(100);

ALTER TABLE host_applications
    ALTER COLUMN display_name DROP NOT NULL;

UPDATE host_applications
SET torn_name = COALESCE(NULLIF(BTRIM(torn_name), ''), NULLIF(BTRIM(display_name), ''), 'Unknown')
WHERE torn_name IS NULL OR BTRIM(torn_name) = '';

ALTER TABLE host_applications
    ALTER COLUMN torn_name SET NOT NULL;
