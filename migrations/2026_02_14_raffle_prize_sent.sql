ALTER TABLE public.raffles
  ADD COLUMN IF NOT EXISTS announcement_channel_id bigint,
  ADD COLUMN IF NOT EXISTS announcement_message_id bigint,
  ADD COLUMN IF NOT EXISTS purchase_channel_id bigint,
  ADD COLUMN IF NOT EXISTS purchase_message_id bigint,
  ADD COLUMN IF NOT EXISTS cleaned_at timestamptz,
  ADD COLUMN IF NOT EXISTS prize_sent_at timestamptz,
  ADD COLUMN IF NOT EXISTS winner_torn_id integer,
  ADD COLUMN IF NOT EXISTS prize_confirm_dm_channel_id bigint,
  ADD COLUMN IF NOT EXISTS prize_confirm_dm_message_id bigint;
