ALTER TABLE public.guild_settings
ADD COLUMN IF NOT EXISTS raffle_giveaway_purchase_channel_id bigint;
