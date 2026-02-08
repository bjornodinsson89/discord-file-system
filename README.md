# Happy Jumper vNext - Complete Refactored System

> Deployment note: see `DEPLOYMENT.md` for Railway split-service commands and per-service env vars.

**Version 2.0.0** - Dashboard-First Admin & Creation Panel

This is the complete refactored version of Happy Jumper Bot with integrated web dashboard, following the specification document requirements.

## 🎯 What's New

### Architecture Changes
- **Split Railway Services**: Web/API and Discord bot run as separate process types/services
- **Migrations System**: Versioned SQL migrations for safe schema evolution
- **FastAPI Backend**: Modern async Python web framework for dashboard API
- **React Frontend**: Beautiful SaaS-style dashboard with Tailwind CSS + shadcn/ui
- **Discord OAuth2**: Secure authentication with guild-based permission checking

### Updated Form Specifications

#### 99k Sessions (7.1)
- **Xanax Stack**: Changed from numeric count to: `1 xanax`, `2 xanax`, `3 xanax`, or `full stack`
- Validation: 1-3 individual xanax or full stack option

#### Raffles (7.2)
- **Tickets Available**: Changed from "Max tickets" to clearly show total available
- Freeform prize description (paragraph-friendly)
- Max tickets per user (0 = unlimited)

#### Insurance Policies (7.3)
- **Cost**: Item type + amount (replaces "Premium Amount")
- **Coverage Type**: `xanax_stack`, `ecstasy_after_stack`, or `all_drugs` (replaces "Max coverage")
- **Payout**: Freeform description (replaces "Payout per xanax")
- Provider workflow with approval system

## 📋 Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend build)
- PostgreSQL database (Supabase recommended)
- Discord Bot Token + OAuth2 Application
- Railway account (for deployment)

## 🚀 Quick Start (Local Development)

### 1. Clone and Setup

```bash
# Clone the refactored codebase
cd happy-jumper-refactored

# Install Python dependencies
pip install -r requirements.txt

# Install frontend dependencies
cd frontend
npm install
cd ..
```

### 2. Environment Variables

Create a `.env` file:

```env
# Discord Configuration
DISCORD_TOKEN=your_bot_token_here  # Required by both bot and web for guild/bot-presence checks
DISCORD_CLIENT_ID=your_client_id_here
DISCORD_CLIENT_SECRET=your_client_secret_here
GUILD_ID=your_test_guild_id  # Optional: for faster command sync

# Dashboard Configuration
DASHBOARD_URL=http://localhost:8000
DASHBOARD_SECRET_KEY=generate_a_random_32_char_string_here  # Must remain stable across deploys
FRONTEND_URL=http://localhost:8000

# Database Configuration (Supabase)
DB_HOST=db.your-project.supabase.co
DB_PORT=6543  # Supabase connection pooler port
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your_database_password
DB_SSL=require

# Security
FERNET_KEY=generate_using_python_cryptography_fernet
```

### 3. Generate Security Keys

```python
# Generate Fernet Key
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())

# Generate Dashboard Secret
import secrets
print(secrets.token_urlsafe(32))
```

### 4. Build Frontend

```bash
cd frontend
npm run build
cd ..

This build step is required for production deployments. Railway will run it automatically
as part of the build command in `railway.json`.
```

### 5. Run Database Migrations

The migrations will run automatically on first startup, or you can run them manually:

```python
import asyncio
import asyncpg
from migrations.migration_runner import run_migrations
import config

async def main():
    pool = await asyncpg.create_pool(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        ssl=config.DB_SSL
    )
    count = await run_migrations(pool)
    print(f"Applied {count} migrations")
    await pool.close()

asyncio.run(main())
```

### 6. Start Services (Local)

```bash
# Terminal 2: bot service
python bot.py

# Terminal 3: web/api service (FastAPI only)
uvicorn api.main:app --host 0.0.0.0 --port 8000
```


### Quick local checks

```bash
python -m py_compile web/app.py web/permissions.py web/discord_api.py admin_api/routes.py utils/database.py
uvicorn api.main:app --host 0.0.0.0 --port 8000
curl -s http://127.0.0.1:8000/api/health
python bot.py
```

### 7. Access Dashboard

1. Navigate to `http://localhost:8000`
2. Click "Login with Discord"
3. Authorize the application
4. You'll be redirected to the dashboard

## 🚆 Railway split-service startup

Run **two Railway services** from the same repo:

- **WEB service**: `SERVICE_MODE=WEB` and start command `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- **BOT service**: `SERVICE_MODE=BOT` and start command `python bot.py`

`railway.json` defaults to the WEB command unless `START_COMMAND` is overridden per-service.

## 📂 Project Structure

```
happy-jumper-refactored/
├── bot.py                    # Bot process entry point
├── config.py                 # Configuration with OAuth settings
├── requirements.txt          # Python dependencies
├── Procfile                  # Railway deployment config
├── runtime.txt              # Python version specification
│
├── migrations/              # Versioned SQL migrations
│   ├── migration_runner.py  # Migration system
│   ├── 001_initial_schema.sql
│   └── 002_vnext_updates.sql
│
├── utils/                   # Shared utilities
│   ├── database.py          # Database operations
│   ├── torn_api.py          # Torn API integration
│   ├── embeds.py            # Discord embed builders
│   └── security.py          # Encryption/decryption
│
├── views/                   # Discord UI views
│   └── __init__.py          # All Discord interactive components
│
├── web/                     # FastAPI web application
│   ├── app.py               # Main FastAPI app
│   ├── auth.py              # Discord OAuth2
│   └── permissions.py       # Admin permission checking
│
├── admin_api/               # Admin API endpoints
│   ├── schemas.py           # Pydantic request/response models
│   ├── handlers.py          # "Post to Discord" integration
│   └── routes.py            # FastAPI route definitions
│
└── frontend/                # React dashboard
    ├── src/
    │   ├── components/      # Reusable UI components
    │   ├── pages/           # Dashboard pages
    │   ├── lib/             # Utilities and API client
    │   └── hooks/           # Custom React hooks
    ├── package.json
    ├── vite.config.js
    └── tailwind.config.js
```

## 🔧 Development Workflow

### Running in Development Mode

```bash
# Terminal 1: Frontend dev server with hot reload
cd frontend
npm run dev

# Terminal 2: Backend with auto-reload
python bot.py
```

### Making Database Changes

1. Create a new migration file: `migrations/003_description.sql`
2. Write your SQL changes
3. Restart the service - migrations run automatically
4. For rollback, create `003_down.sql` (optional)

### Adding New API Endpoints

1. Define schemas in `admin_api/schemas.py`
2. Implement handler in `admin_api/handlers.py`
3. Add route in `admin_api/routes.py`
4. Update frontend API client

## 🚢 Deployment to Railway

### Railway Configuration

1. **Create New Project** in Railway dashboard

2. **Add Postgres Database** (or connect existing Supabase)

3. **Deploy from GitHub**:
   - Connect your repository
   - Railway auto-detects Python
   - Uses `Procfile` for command

4. **Environment Variables**:
   Set all variables from `.env` in Railway dashboard

5. **Generate Domain**:
   - Railway provides a domain like `your-app.railway.app`
   - Update `DASHBOARD_URL` and `FRONTEND_URL` to match

6. **Discord OAuth Configuration**:
   - Update redirect URI in Discord Developer Portal:
   - `https://your-app.railway.app/auth/callback`

### Procfile

```
web: uvicorn web.app:app --host 0.0.0.0 --port $PORT
bot: python bot.py
```

### Railway.json (Optional)

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": {
    "builder": "NIXPACKS",
    "buildCommand": "pip install -r requirements.txt && cd frontend && npm install && npm run build"
  },
  "deploy": {
    "startCommand": "uvicorn web.app:app --host 0.0.0.0 --port $PORT",
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

## 📱 Dashboard Features

### Pages

- **Overview**: KPIs, recent actions, quick create buttons
- **Sessions**: List, filter, create 99k jump sessions
- **Raffles**: List, create raffles, draw winners
- **Insurance**: Provider management, policy creation, claims
- **Settings**: Guild configuration, test tools
- **Audit Log**: Searchable activity history

### Permissions

- **Administrator**: Full access to all features
- **Admin Role**: Configurable role_id with full access
- **Providers**: Can create and manage their own policies

## 🔐 Security Features

- Discord OAuth2 with state parameter (CSRF protection)
- Session-based authentication
- Guild membership verification
- Permission checks on every API call
- API key encryption at rest (Fernet)
- SQL injection protection (parameterized queries)
- Rate limiting ready (can be added)

## 🐛 Troubleshooting

### Migrations Not Running
```python
# Manually run migrations
from utils import get_database
from migrations.migration_runner import run_migrations

db = await get_database()
count = await run_migrations(db.pool)
```

### Frontend Not Loading
```bash
# Rebuild frontend
cd frontend
rm -rf dist node_modules
npm install
npm run build
```

### Bot Commands Not Syncing
```bash
# Clear and resync commands
# Set GUILD_ID in .env for faster testing
# Or use global sync (takes up to 1 hour)
```

### Database Connection Issues
- Verify Supabase connection pooler settings
- Check SSL mode is set to "require"
- Ensure IP whitelisting if enabled

## 📚 Additional Resources

- [Discord.py Documentation](https://discordpy.readthedocs.io/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [Railway Documentation](https://docs.railway.app/)
- [Supabase Documentation](https://supabase.com/docs)

## 🤝 Contributing

When contributing:
1. Create migrations for schema changes
2. Update schemas.py for API changes
3. Follow existing code patterns
4. Test locally before deploying
5. Update documentation

## 📄 License

[Your License Here]

## 🎉 Credits

Built with love for the Torn City community.

---

**Need Help?** Open an issue or contact the development team.

## Deployment topology (authoritative)

This codebase requires **2 services** in Railway:

- **WEB** (`uvicorn api.main:app --host 0.0.0.0 --port $PORT`) for dashboard + OAuth + API.
- **BOT** (`python bot.py`) for Discord gateway events and slash commands.

`bot_internal` is legacy and not required by active WEB routes.

## Environment variable audit

All environment variables referenced in code:

- `DISCORD_TOKEN`
- `GUILD_ID`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `BOT_SERVICE_URL` (legacy)
- `BOT_INTERNAL_SECRET` (legacy)
- `BOT_INTERNAL_HOST` (legacy)
- `BOT_INTERNAL_PORT` (legacy)
- `SERVICE_MODE`
- `RUN_WEB`
- `RUN_BOT`
- `RUN_BOT_INTERNAL` (legacy)
- `DASHBOARD_URL`
- `FRONTEND_URL`
- `DASHBOARD_SECRET_KEY`
- `OAUTH_REDIRECT_URI`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`
- `DB_SSL`
- `RUN_MIGRATIONS`
- `FERNET_KEY`
- `SESSION_COOKIE_SAMESITE`
- `SESSION_COOKIE_SECURE`

### Service scoping

- **Shared required**: `DISCORD_TOKEN`, DB vars (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, optional `DB_SSL`), `FERNET_KEY`.
- **WEB required**: `DASHBOARD_URL`, `FRONTEND_URL`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DASHBOARD_SECRET_KEY`.
- **BOT required**: shared only.
- **Legacy not required by active flow**: `BOT_SERVICE_URL`, `BOT_INTERNAL_SECRET`, `BOT_INTERNAL_HOST`, `BOT_INTERNAL_PORT`, `RUN_BOT_INTERNAL`.

## Security / DB access notes

- Dashboard writes are server-side only through FastAPI routes (no direct client DB writes).
- Web auth uses Discord OAuth session and server-side guild permission checks.
- Blacklist functionality is dashboard API only; bot slash blacklist commands are not registered.

### Recommended Supabase RLS posture

If using Supabase directly for SQL access, keep app writes behind server credentials and apply restrictive RLS:

```sql
-- Example baseline: deny by default
alter table public.blacklist enable row level security;
create policy "deny_all_by_default" on public.blacklist
for all using (false) with check (false);

-- If you later expose read access via Supabase auth claims, create explicit per-guild policies.
```

For this app, preferred approach is still: DB access from backend only with privileged server credentials.
