# Happy Jumper

Happy Jumper runs as **exactly 2 Railway services**:

1. **Web service**: FastAPI + dashboard frontend
   - Start command: `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
2. **Bot service**: Discord bot worker
   - Start command: `python bot.py`

> `bot_internal/` and `BOT_SERVICE_URL`/`BOT_INTERNAL_SECRET` are retired legacy concepts and are no longer used.

## Required environment variables

### Shared (Web + Bot)
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `FERNET_KEY`
- `DISCORD_TOKEN`

### Web-only
- `SERVICE_MODE=WEB`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DASHBOARD_SECRET_KEY`
- `OAUTH_REDIRECT_URI`
- `FRONTEND_URL`

### Bot-only
- `SERVICE_MODE=BOT`
- `GUILD_ID` *(optional; when set, bot syncs only guild commands)*
- `ADMIN_ROLE_NAME` *(optional; `/dashboard` is allowed for this role OR Discord Administrator)*
- `CLEAN_COMMANDS` *(optional; set `1` for one-time stale command cleanup)*

## Duplicate slash-command cleanup playbook

Duplicate commands happen when old global + guild registrations both exist.

### Production target (global-only)
1. On bot service, **unset `GUILD_ID`**.
2. Set `CLEAN_COMMANDS=1`.
3. Restart bot once.
4. Confirm logs show `scope=global` and cleanup messages.
5. Set `CLEAN_COMMANDS=0` (or remove it) and restart bot normally.

### Development target (guild-only)
1. Set `GUILD_ID=<your_dev_guild_id>`.
2. Set `CLEAN_COMMANDS=1`.
3. Restart bot once.
4. Confirm logs show `scope=guild:<id>` and global cleanup.
5. Set `CLEAN_COMMANDS=0` (or remove it) and restart bot normally.

## Permissions model

- `/dashboard` command: requires **Discord Administrator** OR a role matching `ADMIN_ROLE_NAME`.
- Dashboard guild list: only guilds where the OAuth user has **Administrator** and the bot is present.
- API routes that accept `guild_id` enforce `require_guild_admin()`.

## Welcome message settings

The welcome system is powered by bot event `on_member_join` and guild settings in DB:
- `welcome_enabled`
- `welcome_channel_id`
- `welcome_message_template`

These are editable in Dashboard → Settings. Channel dropdown data is loaded via Discord API using the bot token.

## Blacklist policy

Blacklist management is **dashboard-only**. There are no slash/message commands for blacklist actions.

## Healthcheck

Web healthcheck endpoints:
- `/health`
- `/api/health`

## Local run

```bash
pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..

# bot
SERVICE_MODE=BOT python bot.py

# web
SERVICE_MODE=WEB uvicorn web.app:app --host 0.0.0.0 --port 8000
```
