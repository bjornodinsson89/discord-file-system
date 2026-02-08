# Railway Deployment: 3 Services (WEB, BOT, BOT-INTERNAL)

Deploy this repository as **three Railway services** from the same repo:

1. **WEB** (dashboard + admin API)
2. **BOT** (Discord gateway bot only)
3. **BOT-INTERNAL** (FastAPI internal bridge to Discord REST)

## Railway project setup

- Keep one repo, create 3 services in Railway from it.
- Set each service's `START_COMMAND` separately.
- `railway.json` uses `START_COMMAND` so each service can boot independently.

### Service start commands

- **WEB**
  - `START_COMMAND=uvicorn web.app:app --host 0.0.0.0 --port $PORT`
  - `SERVICE_MODE=WEB`
- **BOT**
  - `START_COMMAND=python bot.py`
  - `SERVICE_MODE=BOT`
- **BOT-INTERNAL**
  - `START_COMMAND=uvicorn bot_internal.app:app --host 0.0.0.0 --port $PORT`
  - `SERVICE_MODE=BOT_INTERNAL`

## Exact environment variables by service

## WEB

Required:
- `SERVICE_MODE=WEB`
- `DASHBOARD_URL`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `BOT_SERVICE_URL` (URL of BOT-INTERNAL service)
- `BOT_INTERNAL_SECRET` (shared secret)

Typical optional:
- `FRONTEND_URL` (if unset, defaults in app behavior)
- `OAUTH_REDIRECT_URI` (if unset, built from `DASHBOARD_URL`)
- DB vars (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL`) only if your WEB flow uses DB-backed features
- `FERNET_KEY` if using encrypted secret storage from WEB routes

## BOT-INTERNAL

Required:
- `SERVICE_MODE=BOT_INTERNAL`
- `DISCORD_TOKEN`
- `BOT_INTERNAL_SECRET`

Not required unless you intentionally add DB-backed behavior:
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL`

## BOT

Required:
- `SERVICE_MODE=BOT`
- `DISCORD_TOKEN`

Also required if bot runtime features use DB:
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL`
- `FERNET_KEY` (if bot reads encrypted API keys)

## Networking between WEB and BOT-INTERNAL

- Configure `BOT_SERVICE_URL` on WEB to point to BOT-INTERNAL's reachable URL.
- WEB must send `X-Internal-Secret` and BOT-INTERNAL validates it against `BOT_INTERNAL_SECRET`.
- WEB should never receive `DISCORD_TOKEN`.

## Health checks

- WEB: `GET /api/health`
- BOT-INTERNAL: `GET /internal/health` (requires `X-Internal-Secret`)

## Quick verification commands

```bash
python -m py_compile $(rg --files -g '*.py')
```
