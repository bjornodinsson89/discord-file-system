# Railway deployment (Discord-only)

1. Set environment variables:
   - `DISCORD_TOKEN`
   - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_SSL`
   - `FERNET_KEY`
   - optional: `RUN_MIGRATIONS=1`
2. Railway start command:

```bash
python bot.py
```

3. Deploy single service only (no web service / no frontend).
