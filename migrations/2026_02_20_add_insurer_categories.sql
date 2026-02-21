ALTER TABLE public.insurer_profiles
    ADD COLUMN IF NOT EXISTS categories TEXT[] NOT NULL DEFAULT '{}';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'insurer_profiles_categories_allowed_check'
          AND conrelid = 'public.insurer_profiles'::regclass
    ) THEN
        ALTER TABLE public.insurer_profiles
            ADD CONSTRAINT insurer_profiles_categories_allowed_check
            CHECK (
                categories <@ ARRAY[
                    '99k jump',
                    'Happy jump',
                    'Xanax stack',
                    'Ecstasy only',
                    'Multi day',
                    '2 hours after purchase'
                ]::text[]
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS insurer_profiles_categories_gin
    ON public.insurer_profiles USING GIN (categories);
