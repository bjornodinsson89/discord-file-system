# Railway deployment (Discord-only)

1. Set environment variables:
   - `DISCORD_TOKEN`
   - Database credentials (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`) or `DATABASE_URL`
   - `FERNET_KEY`

2. Configure database SSL mode:
   - Recommended on Railway: `DB_SSL=require` (TLS encryption on, certificate verification off by default)
   - You can omit `DB_SSL` if your `DATABASE_URL` already includes `sslmode=...`; the bot derives `DB_SSL` from that value.
   - Use `DB_SSL=verify-full` (or `verify-ca`) only when you know the provider cert chain is verifiable.
   - Optional override: `DB_SSL_VERIFY=true|false`.

3. Initialize or migrate schema via migration runner:

```bash
# Fresh database install (uses migrations/000_full_schema.sql)

# Existing database migration
```

4. Railway start command:

```bash
python bot.py
```

5. Deploy single service only (no web service / no frontend).
