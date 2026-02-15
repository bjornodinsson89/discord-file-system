ALTER TABLE public.guild_settings
  ADD COLUMN IF NOT EXISTS host_tax_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS host_tax_recipient_torn_id integer,
  ADD COLUMN IF NOT EXISTS host_tax_type text,
  ADD COLUMN IF NOT EXISTS host_tax_item_id integer,
  ADD COLUMN IF NOT EXISTS host_tax_quantity integer,
  ADD COLUMN IF NOT EXISTS host_tax_cash_amount bigint;
