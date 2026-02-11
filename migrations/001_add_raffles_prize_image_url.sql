-- Adds optional external prize image URL for raffle purchase embeds.
ALTER TABLE raffles ADD COLUMN IF NOT EXISTS prize_image_url TEXT;
