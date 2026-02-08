# Railway Deployment (2 Services Only)

## Architecture

Deploy from the same repository into exactly two services:

- **Web** (`SERVICE_MODE=WEB`)
  - Start: `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
- **Bot** (`SERVICE_MODE=BOT`)
  - Start: `python bot.py`

Legacy `bot_internal` service is retired and must not be deployed.

---

## Service setup steps

Do **not** use legacy `RUN_WEB` / `RUN_BOT` flags. Use `SERVICE_MODE` only.

1. Create Railway project and attach your database.
2. Create **Web** service from repo.
3. Create **Bot** service from same repo.
4. Set start commands exactly as above (or use the repo default Railway start command that branches on `SERVICE_MODE`).
5. Set env vars per service.

### Shared env vars (set in both services)
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`
- `DISCORD_TOKEN`
- `DB_SSL_CA_FILE` (optional; only for `verify-ca`/`verify-full`)

### Web-only env vars
- `SERVICE_MODE=WEB`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `OAUTH_REDIRECT_URI`
- `FRONTEND_URL`

### Bot-only env vars
- `SERVICE_MODE=BOT`
- `GUILD_ID` (optional)
- `ADMIN_ROLE_NAME` (optional)
- `CLEAN_COMMANDS` (optional)

---

### Database SSL (`DB_SSL`)

`asyncpg` expects either `ssl=None` or a configured `SSLContext`. Set `DB_SSL` using one of:

- `disable`: no TLS (`ssl=None`).
- `require` / `prefer` / `allow`: TLS enabled, but certificate chain is **not** verified (Postgres `sslmode=require` behavior).
- `verify-ca`: TLS with certificate-chain verification, hostname check disabled.
- `verify-full`: TLS with certificate-chain and hostname verification.

Optional: set `DB_SSL_CA_FILE` to a CA bundle path when using `verify-ca`/`verify-full`. If set but unreadable, startup fails fast with a clear error.

**Recommended Railway value:** `DB_SSL=require` (encrypted transport without certificate verification, compatible with self-signed chains).

---

## Remove duplicate slash commands

### Global-only production mode
- Ensure `GUILD_ID` is **unset**.
- Set `CLEAN_COMMANDS=1`.
- Restart bot once.
- Wait for sync to complete.
- Remove/set `CLEAN_COMMANDS=0`.
- Restart bot again.

### Guild-only development mode
- Set `GUILD_ID=<dev guild id>`.
- Set `CLEAN_COMMANDS=1`.
- Restart bot once.
- Remove/set `CLEAN_COMMANDS=0`.
- Restart bot again.

Bot startup logs now print which scope is syncing (`global` vs `guild:<id>`).

---

## Railway screenshots checklist

Capture and store screenshots for your runbook:

1. **Service list** showing only 2 services (Web + Bot).
2. **Web service settings** showing start command.
3. **Bot service settings** showing start command.
4. **Web env vars** panel.
5. **Bot env vars** panel (showing `GUILD_ID`/`CLEAN_COMMANDS` usage when cleaning).
6. **Bot logs** showing command sync scope + cleanup.
7. **Web `/health` response** in browser.

