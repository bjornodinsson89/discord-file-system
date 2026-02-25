BEGIN;

-- 1) Helper function: ensures NEW.guild_id matches the referenced wallet's guild_id
CREATE OR REPLACE FUNCTION public.casino_enforce_wallet_guild_match()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    wallet_guild_id BIGINT;
BEGIN
    SELECT guild_id INTO wallet_guild_id
    FROM public.casino_wallets
    WHERE id = NEW.wallet_id;

    IF wallet_guild_id IS NULL THEN
        RAISE EXCEPTION 'casino wallet not found: wallet_id=%', NEW.wallet_id;
    END IF;

    IF NEW.guild_id IS NULL THEN
        RAISE EXCEPTION 'casino row guild_id is NULL for wallet_id=%', NEW.wallet_id;
    END IF;

    IF wallet_guild_id <> NEW.guild_id THEN
        RAISE EXCEPTION 'casino guild mismatch: row.guild_id=% wallet.guild_id=% wallet_id=%',
            NEW.guild_id, wallet_guild_id, NEW.wallet_id;
    END IF;

    RETURN NEW;
END;
$$;

-- 2) Drop triggers if they already exist (idempotent deploys)
DROP TRIGGER IF EXISTS trg_casino_ledger_wallet_guild_match ON public.casino_ledger;
DROP TRIGGER IF EXISTS trg_casino_deposits_wallet_guild_match ON public.casino_deposits;
DROP TRIGGER IF EXISTS trg_casino_cashouts_wallet_guild_match ON public.casino_cashouts;
DROP TRIGGER IF EXISTS trg_casino_game_rounds_wallet_guild_match ON public.casino_game_rounds;

-- 3) Add triggers to enforce wallet guild match on insert/update
CREATE TRIGGER trg_casino_ledger_wallet_guild_match
BEFORE INSERT OR UPDATE OF guild_id, wallet_id
ON public.casino_ledger
FOR EACH ROW
EXECUTE FUNCTION public.casino_enforce_wallet_guild_match();

CREATE TRIGGER trg_casino_deposits_wallet_guild_match
BEFORE INSERT OR UPDATE OF guild_id, wallet_id
ON public.casino_deposits
FOR EACH ROW
EXECUTE FUNCTION public.casino_enforce_wallet_guild_match();

CREATE TRIGGER trg_casino_cashouts_wallet_guild_match
BEFORE INSERT OR UPDATE OF guild_id, wallet_id
ON public.casino_cashouts
FOR EACH ROW
EXECUTE FUNCTION public.casino_enforce_wallet_guild_match();

CREATE TRIGGER trg_casino_game_rounds_wallet_guild_match
BEFORE INSERT OR UPDATE OF guild_id, wallet_id
ON public.casino_game_rounds
FOR EACH ROW
EXECUTE FUNCTION public.casino_enforce_wallet_guild_match();

COMMIT;
