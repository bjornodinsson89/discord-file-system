# Happy Jumper Discord Bot (Railway Single Service)

This project now runs as a **single Discord bot service** (no FastAPI dashboard, no frontend).

## Start command

```bash
python bot.py
```

## Database schema setup

Use `migration_runner.py` for all schema setup and updates:

- Fresh database install:

```bash
python migrations/migration_runner.py fresh
```

  This applies `migrations/000_full_schema.sql`.

- Existing database updates:

```bash
python migrations/migration_runner.py migrate
```

  This applies numbered incremental migrations after `000_full_schema.sql`.


## Supabase SQL required

Use `migrations/000_full_schema.sql` as the canonical schema. At minimum, ensure these columns exist:

- `guild_settings.announce_channel_id BIGINT`
- `guild_settings.welcome_channel_id BIGINT`
- `guild_settings.admin_role_ids JSONB`
- `guild_settings.raffle_channel_id BIGINT`
- `guild_settings.insurance_channel_id BIGINT`
- `guild_settings.jump_99k_channel_id BIGINT`
- `happy_jump_sessions.xanax_count`

If startup schema validation fails, run the full schema SQL in Supabase SQL editor (or equivalent `ALTER TABLE` statements), then restart the bot.

## Required environment variables

- `DISCORD_TOKEN`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`

Optional:
- `GUILD_ID` (faster guild-scoped slash sync in dev)
- `CLEAN_COMMANDS`
- `RUN_MIGRATIONS`
- `RUN_EMERGENCY_SCHEMA_FIXES`

## Discord-only configuration

`/setup` is the only configuration surface for guild channels, roles, templates, and feature toggles.

Run `/setup` to configure everything (channels, roles, announcement templates, feature toggles, and tests) from one interactive panel.

Permission for setup: guild owner, Administrator, Manage Guild, or a role listed in `guild_settings.admin_role_ids`.

On guild join, the bot auto-selects the first text channel where it can `Send Messages` and `Embed Links` and stores it as `announce_channel_id`.
