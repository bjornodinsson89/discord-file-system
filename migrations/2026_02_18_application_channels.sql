ALTER TABLE public.applications
  ALTER COLUMN thread_id DROP NOT NULL;

ALTER TABLE public.applications
  ADD COLUMN IF NOT EXISTS application_channel_id BIGINT;

UPDATE public.applications
SET application_channel_id = COALESCE(application_channel_id, thread_id);

ALTER TABLE public.applications
  ALTER COLUMN application_channel_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS applications_app_channel_idx
  ON public.applications (application_channel_id);
