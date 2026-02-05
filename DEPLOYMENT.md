# Happy Jumper Bot - Deployment Guide

## Overview

Happy Jumper is a combined Discord bot + FastAPI dashboard system for managing 99k jump sessions, raffles, and insurance in Torn City factions. This guide covers deployment to Railway.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Railway Service                       │
│  ┌─────────────────────────────────────────────────────┐│
│  │                  bot.py (Main)                      ││
│  │  ┌──────────────────┐  ┌─────────────────────────┐ ││
│  │  │   Discord Bot    │  │   FastAPI Dashboard     │ ││
│  │  │  (discord.py)    │  │   (uvicorn thread)      │ ││
│  │  │                  │  │                         │ ││
│  │  │  - Slash cmds    │  │  - OAuth routes         │ ││
│  │  │  - Workers       │  │  - Admin API            │ ││
│  │  │  - Views         │  │  - React SPA            │ ││
│  │  └──────────────────┘  └─────────────────────────┘ ││
│  └─────────────────────────────────────────────────────┘│
│                           │                              │
│                           ▼                              │
│  ┌─────────────────────────────────────────────────────┐│
│  │             PostgreSQL (via PgBouncer)               ││
│  │              (Supabase / Railway DB)                 ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
```

## Prerequisites

1. **Discord Application** with:
   - Bot token
   - OAuth2 Client ID & Secret
   - Redirect URI configured for your domain

2. **PostgreSQL Database** (Railway or Supabase)

3. **Fernet Key** for encryption:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

## Environment Variables

Create these in Railway's service settings:

### Required
```env
# Discord
DISCORD_TOKEN=your_bot_token
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret

# Database (Railway provides DATABASE_URL, or use these)
DB_HOST=your_db_host
DB_PORT=6543  # Use pooler port for Railway/Supabase
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your_db_password
DB_SSL=require

# Security
FERNET_KEY=your_generated_fernet_key

# Dashboard
DASHBOARD_URL=https://your-service.up.railway.app
DASHBOARD_SECRET_KEY=generate_a_random_string  # Must remain stable across deploys
FRONTEND_URL=https://your-service.up.railway.app
```

### Optional
```env
# Development/testing guild (sync commands faster)
GUILD_ID=your_test_guild_id
```

## Railway Deployment Steps

### 1. Create Railway Project

1. Go to [Railway](https://railway.app) and create a new project
2. Choose "Empty Project"

### 2. Add PostgreSQL (or use Supabase)

**Option A: Railway PostgreSQL**
1. Click "New" → "Database" → "PostgreSQL"
2. Use the provided connection details

**Option B: Supabase (recommended for free tier)**
1. Create a Supabase project
2. Go to Settings → Database → Connection Pooling
3. Use the "Transaction" pooler URL with port 6543

### 3. Deploy the Bot

1. Click "New" → "GitHub Repo"
2. Select your repository
3. Railway will auto-detect Python and build

### 4. Configure Build

Create `railway.json` in the repo root (already included in this repo):
```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS",
    "buildCommand": "pip install -r requirements.txt && cd frontend && npm ci && npm run build"
  },
  "deploy": {
    "startCommand": "python bot.py",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

### 5. Set Environment Variables

In Railway, go to your service → Variables tab and add all required environment variables.

### 6. Configure Discord OAuth

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Select your application → OAuth2
3. Add redirect URL: `https://your-service.up.railway.app/auth/callback`
4. Note: URL must match exactly, including trailing slashes

### 7. Invite Bot to Server

Generate invite URL with these scopes:
- `bot`
- `applications.commands`

Permissions needed:
- Send Messages
- Embed Links
- Read Message History
- Add Reactions

Invite URL format:
```
https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=274878024768&scope=bot%20applications.commands
```

## Database Setup

The migration runner automatically runs on startup. For fresh installs:

```bash
# Local testing
python -m migrations.migration_runner fresh

# Verify schema
python -m migrations.migration_runner verify
```

## Monitoring

### Railway Logs
```bash
railway logs --follow
```

### Health Check
The dashboard provides a health endpoint:
```
GET /api/health
→ {"status": "healthy", "service": "happy-jumper"}
```

## Troubleshooting

### "Commands not showing"
- Wait 1 hour for global sync, or set `GUILD_ID` for instant guild sync
- Check bot has `applications.commands` scope

### "Database connection failed"
- Verify `DB_PORT` is the **pooler port** (usually 6543), not direct port (5432)
- Check `statement_cache_size=0` is set (already configured in code)

### "OAuth redirect failed"
- Verify redirect URI in Discord exactly matches `DASHBOARD_URL/auth/callback`
- Check `DASHBOARD_URL` includes `https://`

### "Session expired"
- `DASHBOARD_SECRET_KEY` must be consistent across deploys
- Store it in Railway variables, not generated randomly

### "Dashboard link points at the wrong domain"
- Set `FRONTEND_URL` (and `DASHBOARD_URL`) to the public Railway web domain
- Re-deploy so the `/dashboard` command uses the correct URL

## Scaling

The combined bot+dashboard architecture means:
- Single Railway service to manage
- Shared memory for bot instance access from API
- Background workers run in the same process

For high-traffic scenarios, consider:
- Increasing Railway resource allocation
- Using Railway's horizontal scaling (requires Redis for shared state)

## Security Notes

1. **API Keys are encrypted** at rest using Fernet
2. **Session cookies** use `DASHBOARD_SECRET_KEY` - keep it secret!
3. **OAuth state** validates to prevent CSRF
4. **Guild access** requires Administrator OR configured admin_role_id

## Updates

To deploy updates:
1. Push to your GitHub repository
2. Railway auto-deploys from `main` branch
3. Watch logs for migration results

## File Structure

```
happy-jumper-refactored/
├── bot.py                 # Main entry point
├── config.py              # Environment config
├── admin_api/             # FastAPI routes & handlers
├── frontend/              # React dashboard
│   ├── src/
│   └── dist/              # Built by Railway
├── migrations/            # Database schema
├── utils/                 # Shared utilities
│   ├── database.py
│   ├── torn_api.py
│   ├── security.py
│   └── embeds.py
├── views/                 # Discord UI components
├── web/                   # OAuth & FastAPI app
└── requirements.txt
```
