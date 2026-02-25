ALTER TABLE public.xanax_pools
ADD COLUMN IF NOT EXISTS unlimited_tickets boolean NOT NULL DEFAULT false;

ALTER TABLE public.xanax_pools
ADD COLUMN IF NOT EXISTS end_draw_at timestamptz;

-- allow NULL tickets_total for unlimited pools
ALTER TABLE public.xanax_pools
ALTER COLUMN tickets_total DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_xanax_pools_status_end_draw_at
ON public.xanax_pools(status, end_draw_at);
