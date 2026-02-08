# Railway Split Deployment (Web/API + Bot)

Run this repo as **two Railway services**.

## Service commands

Use per-service `START_COMMAND` so each service starts the right process:

- **Web/API service**
  - `SERVICE_MODE=API`
  - `RUN_MIGRATIONS=true`
  - `START_COMMAND=uvicorn web.app:app --host 0.0.0.0 --port $PORT`
- **Bot service**
  - `SERVICE_MODE=BOT`
  - `RUN_MIGRATIONS=false`
  - `START_COMMAND=python bot.py`

`railway.json` now logs `SERVICE_MODE`, `START_COMMAND`, and `RUN_MIGRATIONS` at boot, then executes `START_COMMAND` when provided.

## Required environment variables

### Shared (both services)

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`
- `BOT_INTERNAL_SECRET`
- `DASHBOARD_URL`

### Web/API-only

- `SERVICE_MODE=API`
- `RUN_MIGRATIONS=true`
- `START_COMMAND=uvicorn web.app:app --host 0.0.0.0 --port $PORT`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `OAUTH_REDIRECT_URI`
- `FRONTEND_URL`
- `BOT_SERVICE_URL` (example: `http://bot-service.railway.internal:8081`)

### Bot-only

- `SERVICE_MODE=BOT`
- `RUN_MIGRATIONS=false`
- `START_COMMAND=python bot.py`
- `DISCORD_TOKEN`
- `GUILD_ID` (optional)
- `BOT_INTERNAL_HOST` (optional, default `0.0.0.0`)
- `BOT_INTERNAL_PORT` (optional, default `8081`)

## Migration compatibility fallback

If your existing database has an older `schema_migrations` table, run:

```sql
ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS description text;
```

## Checklist

1. Create two Railway services from the same repo.
2. Set **shared vars** on both services.
3. Set web-only vars on the API service and bot-only vars on the bot service.
4. Ensure `RUN_MIGRATIONS=true` only on API service.
5. Deploy both services.

## Smoke checks

```bash
python -m py_compile bot.py web/app.py migrations/migration_runner.py utils/database.py config.py
```
