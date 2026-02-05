# Quick Start Guide - Happy Jumper vNext

Get up and running in 15 minutes!

## 🚀 Super Quick Start (Local Development)

### 1. Extract the Archive

```bash
tar -xzf happy-jumper-refactored.tar.gz
cd happy-jumper-refactored
```

### 2. Install Dependencies

```bash
# Python dependencies
pip install -r requirements.txt

# Frontend dependencies
cd frontend
npm install
cd ..
```

### 3. Set Up Environment

```bash
# Copy template
cp .env.example .env

# Edit .env with your credentials
# Required: DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET,
#           DB_HOST, DB_PASSWORD, FERNET_KEY, DASHBOARD_SECRET_KEY
```

### 4. Generate Keys

```bash
# Generate Fernet key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Generate Dashboard secret
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 5. Build Frontend

```bash
cd frontend
npm run build
cd ..
```

### 6. Run the Application

```bash
python bot.py
```

✅ Bot starts on Discord  
✅ Dashboard available at `http://localhost:8000`  
✅ Migrations run automatically

## 🌐 Deploy to Railway (15 minutes)

### 1. Create Railway Project

- Go to [railway.app](https://railway.app)
- Click "New Project" → "Deploy from GitHub"
- Connect your repository

### 2. Add Environment Variables

In Railway dashboard > Variables, add:

```env
DISCORD_TOKEN=your_bot_token
DISCORD_CLIENT_ID=your_client_id
DISCORD_CLIENT_SECRET=your_client_secret
DB_HOST=your_supabase_host
DB_PORT=6543
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your_db_password
DB_SSL=require
FERNET_KEY=generated_key
DASHBOARD_SECRET_KEY=generated_secret
```

### 3. Generate Domain

- Settings → Generate Domain
- Copy the URL (e.g., `your-app.railway.app`)

### 4. Update Variables

Set in Railway:
```env
DASHBOARD_URL=https://your-app.railway.app
FRONTEND_URL=https://your-app.railway.app
```

### 5. Update Discord OAuth

In Discord Developer Portal:
- Add redirect: `https://your-app.railway.app/auth/callback`

### 6. Deploy

Railway automatically deploys! Watch logs for "Bot is ready!"

## 📖 Next Steps

### Verify Deployment

1. Visit your Railway URL
2. Click "Login with Discord"
3. Authorize and access dashboard

### Configure Server

In Discord:
1. Run `/jumper_setup`
2. Set channels and roles
3. Configure settings

### Read the Docs

- **README.md** - Complete guide
- **DEPLOYMENT.md** - Detailed deployment
- **MIGRATION_GUIDE.md** - Upgrading from v1
- **PROJECT_SUMMARY.md** - What was built
- **FRONTEND_TODO.md** - Frontend implementation

## 🔥 What Works Now

### Backend (100% Complete)
- ✅ Discord bot with all commands
- ✅ FastAPI web server
- ✅ OAuth authentication
- ✅ Admin API endpoints
- ✅ Database migrations
- ✅ Background workers

### Frontend (Structure Ready)
- ✅ Login page works
- ✅ OAuth flow complete
- ✅ Layout and routing
- ⚠️ Component pages need implementation (see FRONTEND_TODO.md)

## 🎯 Immediate Testing

Once deployed, test:

```bash
# Discord commands
/ping
/help
/set_api_key

# Dashboard
1. Login via OAuth
2. See your guilds
3. Navigate pages (placeholders for now)
```

## 📊 File Count

**46 files** created including:
- 13 Python files
- 18 Frontend files
- 7 Documentation files
- 8 Configuration files

## 🆘 Troubleshooting

**Bot won't start:**
- Check all environment variables set
- Verify database connection
- Check Railway logs

**OAuth fails:**
- Verify redirect URI matches exactly
- Check CLIENT_ID and CLIENT_SECRET
- Ensure bot invited to server

**Frontend blank:**
- Run `npm run build` in frontend/
- Check frontend/dist/ exists
- Verify build completed successfully

## 🎓 Learning Path

1. **Week 1:** Deploy and test backend
2. **Week 2:** Implement frontend components
3. **Week 3:** Test with users
4. **Week 4:** Refine and optimize

## 💬 Support

- Read comprehensive docs (README.md, DEPLOYMENT.md)
- Check logs: `railway logs` or local console
- Review PROJECT_SUMMARY.md for architecture

---

**You have everything you need to deploy and run Happy Jumper vNext!**

The backend is production-ready. Frontend needs component implementation (20-40 hours for experienced React dev).

Good luck! 🚀
