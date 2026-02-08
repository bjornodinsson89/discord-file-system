# Railway Deployment (2 Services)

This repository now deploys as **2 Railway services**:

1. **WEB**: FastAPI dashboard + Discord OAuth + dashboard API.
2. **BOT**: Discord gateway bot process (slash commands + workers + welcome messages).

`bot_internal/` remains in the repo for legacy compatibility but is **not required** by current code paths.

## Start commands

- **WEB service**
  - `SERVICE_MODE=WEB`
  - `START_COMMAND=uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- **BOT service**
  - `SERVICE_MODE=BOT`
  - `START_COMMAND=python bot.py`

## Environment variables

### Shared (both services)

- `DISCORD_TOKEN`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL` (recommended: `require`)
- `FERNET_KEY`
- `RUN_MIGRATIONS` (recommended: `true` for WEB only; can be unset on BOT)
- `GUILD_ID` (optional dev guild command sync target)

### WEB-only

- `DASHBOARD_URL`
- `FRONTEND_URL`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `OAUTH_REDIRECT_URI` (optional override)
- `SESSION_COOKIE_SAMESITE` (optional)
- `SESSION_COOKIE_SECURE` (optional)

### BOT-only

No additional required variables beyond shared.

### Legacy / not required in 2-service mode

- `BOT_SERVICE_URL`
- `BOT_INTERNAL_SECRET`
- `BOT_INTERNAL_HOST`
- `BOT_INTERNAL_PORT`
- `RUN_BOT_INTERNAL`

## Command sync / duplicate command cleanup

The bot now uses a single command sync path. For guild-scoped dev (`GUILD_ID` set), startup performs a one-time clear+sync to remove stale guild commands before syncing current commands.

## Troubleshooting

- **Missing env var at boot**: startup now fails fast with an explicit list.
- **Slash commands duplicated**:
  1. Ensure only one BOT Railway service is running.
  2. Keep `GUILD_ID` set in development so clear+sync runs on startup.
- **Dashboard guild dropdown empty**:
  - User must have `Administrator` or `Manage Server`.
  - Bot must still be present in that guild.
- **Welcome messages not sent**:
  - Configure `welcome_channel_id` + `welcome_enabled` in dashboard settings.
  - Confirm bot has `View Channel` + `Send Messages` in that channel.
