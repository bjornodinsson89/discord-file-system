# Happy Jumper Discord Bot (Railway Single Service)

This project now runs as a **single Discord bot service** (no FastAPI dashboard, no frontend).

## Start command

```bash
python bot.py
```

## Database schema setup

- Fresh database install:

```bash
psql "$DATABASE_URL" -f migrations/000_full_schema.sql
```

- Existing database updates (after baseline):

```bash
for f in migrations/*.sql; do
  [ "$(basename "$f")" = "000_full_schema.sql" ] && continue
  psql "$DATABASE_URL" -f "$f"
done
```

## Required environment variables

- `DISCORD_TOKEN`
- `DATABASE_URL` (or `DB_HOST` + `DB_PORT` + `DB_NAME` + `DB_USER` + `DB_PASSWORD`)
- `DB_SSL` (`disable`, `require`, `verify-ca`, `verify-full`)
- `DB_SSL_VERIFY` (default `true`; set `false` only for local/dev)
- `FERNET_KEY`

Optional:
- `GUILD_ID` (faster guild-scoped slash sync in dev)
- `CLEAN_COMMANDS`

## Discord-only configuration

`/setup` is the only configuration surface for guild channels, roles, templates, and feature toggles.

Run `/setup` to configure everything (channels, roles, announcement templates, feature toggles, and tests) from one interactive panel.

Permission for setup: guild owner, Administrator, Manage Guild, or a role listed in `guild_settings.admin_role_ids`.

On guild join, the bot auto-selects the first text channel where it can `Send Messages` and `Embed Links` and stores it as `announce_channel_id`.
