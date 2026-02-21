# Railway deployment (Discord-only)

## 1) Required environment variables

- `DISCORD_TOKEN`
- `FERNET_KEY`
- One DB configuration mode:
  - `DATABASE_URL`, or
  - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`

Recommended hardening flags:

- `DB_SSL=require`
- `DB_SSL_VERIFY=true`
- `DB_CONNECT_MAX_ATTEMPTS=20`
- `DB_ACQUIRE_TIMEOUT=10`
- `DB_STATEMENT_TIMEOUT_MS=15000`

## 2) Database schema bootstrap / migrations

Fresh database install:

```bash
psql "$DATABASE_URL" -f migrations/000_full_schema.sql
```

Apply incremental migrations:

```bash
for f in migrations/*.sql; do
  [ "$(basename "$f")" = "000_full_schema.sql" ] && continue
  psql "$DATABASE_URL" -f "$f"
done
```

## 3) Startup command

Railway service start command:

```bash
python bot.py
```

`bot.py` runs both:
- Discord bot process
- Health endpoint server on `0.0.0.0:$PORT` (`/health`)

## 4) Post-deploy verification

- Confirm Railway health checks are green (`/health` returns JSON with `status=ok` and `db=ok`).
- Confirm bot presence in Discord and command responsiveness.
- Check logs for:
  - `Database pool initialized`
  - `Starting Discord bot service`
  - No repeating supervisor crash loops.

## 5) Rollback guidance

- Re-deploy the previous successful Railway deployment.
- If rollback includes schema-affecting release, ensure the previous app version is compatible with current schema before rollout.
- Validate `/health` and a smoke command after rollback.
