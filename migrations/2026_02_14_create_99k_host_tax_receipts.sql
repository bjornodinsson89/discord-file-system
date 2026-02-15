CREATE TABLE IF NOT EXISTS public.host_tax_receipts (
  id bigserial PRIMARY KEY,
  guild_id bigint NOT NULL,
  discord_user_id bigint NOT NULL,
  session_id bigint NULL,
  recipient_torn_id integer NOT NULL,
  tax_type text NOT NULL CHECK (tax_type IN ('item','cash')),
  item_id integer NULL,
  quantity integer NULL,
  cash_amount bigint NULL,
  torn_log_id text NOT NULL,
  paid_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_host_tax_receipts_recent
  ON public.host_tax_receipts (guild_id, discord_user_id, paid_at DESC);
