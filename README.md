# Happy Jumper Discord Bot (Railway Single Service)

This project now runs as a **single Discord bot service** (no FastAPI dashboard, no frontend).

## Start command

```bash
python bot.py
```

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
- `DASHBOARD_SECRET_KEY` (legacy encrypted data compatibility)

## Discord-only configuration commands

- `/setup` – show current config and setup help
- `/setchannel [channel]` – set announce channel (defaults to current channel)
- `/config` – read-only view of config
- `/testannounce` – send a test message to configured announce channel

Permission for config commands: guild owner, Administrator, Manage Guild, or a role listed in `guild_settings.admin_role_ids`.

On guild join, the bot auto-selects the first text channel where it can `Send Messages` and `Embed Links` and stores it as `announce_channel_id`.
