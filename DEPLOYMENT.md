# Railway Split Deployment (Web + Bot)

This project is deployed as **two Railway services** from the same repository.

## Start commands

- **Web/API service**
  - Start command: `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
  - Recommended env: `RUN_WEB=true`, `RUN_BOT=false`
- **Bot service**
  - Start command: `python bot.py`
  - Recommended env: `RUN_WEB=false`, `RUN_BOT=true`

For local development, defaults are backwards-compatible (`RUN_WEB=true`, `RUN_BOT=true`).

## Environment variables

### Shared variables (set on both services)

- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`

### Web/API-only variables

- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `DASHBOARD_URL`
- `FRONTEND_URL`
- `OAUTH_REDIRECT_URI` (recommended; defaults to `${DASHBOARD_URL}/auth/callback`)

### Bot-only variables

- `DISCORD_TOKEN`
- `GUILD_ID` (optional; only for faster test-guild slash command sync)

## Procfile

```procfile
web: uvicorn web.app:app --host 0.0.0.0 --port $PORT
bot: python bot.py
```

## Why this split matters

- Prevents bot/API event-loop cross-talk and thread-loop issues.
- Avoids circular imports caused by package-level side effects.
- Allows each service to validate only the env vars it actually needs.
