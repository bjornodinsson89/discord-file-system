ALTER TABLE public.raffles
ADD COLUMN IF NOT EXISTS admin_comments TEXT NULL;
