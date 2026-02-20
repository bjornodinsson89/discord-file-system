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


## How to run tests locally

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-dev.txt
ruff check .
mypy
pytest -q
```

Notes:
- `mypy` is intentionally scoped to `config.py` and `utils/` in this phase.
- If you only want behavior checks, run `pytest -q`.

## CI status

GitHub Actions workflow `.github/workflows/tests.yml` runs on every push and pull request. It installs runtime + dev dependencies, runs `ruff check .`, runs `mypy` in warn-only mode, and executes `pytest -q`.

## Dependency bump plan

Use `requirements-dev.txt` for tooling updates and keep runtime pins in `requirements.txt` unchanged unless behavior is validated.

Suggested safe bump process:
1. Update one dependency at a time in `requirements-dev.txt`.
2. Run `ruff check .`, `mypy`, and `pytest -q` locally.
3. Open a PR and confirm CI passes before merging.
4. For runtime dependency changes in `requirements.txt`, include a short rollback note and confirm no behavior changes via tests.
