# Railway deployment (Discord-only)

1. Set environment variables:
   - `DISCORD_TOKEN`
   - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL`
   - `FERNET_KEY`
   - optional: `RUN_MIGRATIONS=1`

2. Initialize or migrate schema via migration runner:

```bash
# Fresh database install (uses migrations/000_full_schema.sql)
python migrations/migration_runner.py fresh

# Existing database migration
python migrations/migration_runner.py migrate
```

3. Railway start command:

```bash
python bot.py
```

4. Deploy single service only (no web service / no frontend).
