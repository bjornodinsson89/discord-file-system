# Railway Split Deployment (Web + Bot)

This project runs as **two Railway services** from the same repository:

- **Web service** (Discord OAuth + dashboard APIs)
- **Bot service** (Discord bot + privileged internal API)

The web service must **not** have `DISCORD_TOKEN`.

## Start commands

- **Web service**
  - `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
- **Bot service**
  - `python bot.py`
  - `bot.py` now starts the Discord bot and the internal API server in-process.

## Environment variables

### Shared (both services)

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`
- `BOT_INTERNAL_SECRET`

### Web-only

- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `DASHBOARD_URL`
- `OAUTH_REDIRECT_URI`
- `FRONTEND_URL`
- `BOT_SERVICE_URL` (example: `http://bot-service.railway.internal:8081`)

### Bot-only

- `DISCORD_TOKEN`
- `GUILD_ID` (optional, test-guild slash sync)
- `BOT_INTERNAL_HOST` (optional, default `0.0.0.0`)
- `BOT_INTERNAL_PORT` (optional, default `8081`)

## Internal API authentication

All `/internal/*` routes require:

- Header: `X-Internal-Secret`
- Value: exact `BOT_INTERNAL_SECRET`

Requests with missing/invalid secret are rejected.

## Setup checklist

1. Create two Railway services from this repo (`web`, `bot`).
2. Set shared env vars on both services.
3. Set web-only and bot-only vars on the correct service.
4. Set bot internal port to `8081` (or matching value in `BOT_SERVICE_URL`).
5. Deploy both services.

## Quick smoke checks

```bash
python -m py_compile bot.py bot_internal/app.py web/internal_bot_client.py web/permissions.py admin_api/routes.py utils/database.py views/__init__.py
npm --prefix frontend run build
```
