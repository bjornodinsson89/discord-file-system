# Migration Guide - Upgrading to vNext

Guide for migrating from the original Happy Jumper bot to the dashboard-enabled vNext version.

## ⚠️ Important Notes

- **Database Schema Changes**: vNext includes significant schema updates
- **New Dependencies**: FastAPI, frontend tooling required
- **OAuth Required**: New Discord OAuth2 credentials needed
- **Backward Compatible**: Existing Discord commands still work
- **Zero Downtime**: Follow this guide for seamless migration

## Pre-Migration Checklist

- [ ] **Backup your database** (critical!)
- [ ] Review current guild settings and configurations
- [ ] Note all active sessions, raffles, and policies
- [ ] Have Discord bot token ready
- [ ] Have database credentials ready

## Migration Path Options

### Option A: Fresh Installation (Recommended)

**Best for:** New deployments or testing environments

1. Deploy vNext to new Railway service
2. Configure OAuth and dashboard
3. Test thoroughly
4. When ready, migrate data from old system
5. Switch DNS/domains
6. Retire old service

**Pros:**
- Zero risk to existing system
- Full testing before cutover
- Easy rollback

**Cons:**
- Need to migrate data manually
- Temporary dual setup

### Option B: In-Place Upgrade

**Best for:** Small servers with maintenance windows

1. Announce maintenance window to users
2. Stop old bot service
3. Backup database
4. Deploy vNext code
5. Run migrations
6. Test and verify
7. Resume service

**Pros:**
- Single deployment
- No data migration needed

**Cons:**
- Downtime required
- Cannot easily rollback
- Higher risk

## Step-by-Step Migration (Option A - Recommended)

### Phase 1: Setup vNext Environment

1. **Create New Railway Service**
   ```bash
   # In Railway dashboard
   # Create new project: "happy-jumper-vnext"
   ```

2. **Use Existing Database**
   ```env
   # Point to your current Supabase database
   DB_HOST=your-existing-supabase-host
   DB_PORT=6543
   DB_NAME=postgres
   DB_USER=postgres
   DB_PASSWORD=your-existing-password
   ```

3. **Configure OAuth**
   ```
   # Add new redirect URI in Discord Developer Portal
   https://your-vnext-app.railway.app/auth/callback
   
   # Keep existing bot token - same bot, new features!
   DISCORD_TOKEN=your-existing-token
   ```

4. **Deploy vNext**
   ```bash
   # Push code to GitHub
   # Railway auto-deploys
   ```

### Phase 2: Run Migrations

vNext will automatically run migrations on startup:

```
✓ Migration 001: Initial Schema (skipped - already exists)
✓ Migration 002: vNext Updates (applying...)
  - Added xanax_stack column
  - Migrated existing xanax_count data
  - Added insurance coverage_type column
  - Renamed max_tickets to tickets_available
  - Added dashboard metadata columns
✓ All migrations complete!
```

**Migration 002 is Safe:**
- Adds new columns (doesn't drop existing ones)
- Migrates data automatically
- Maintains backward compatibility
- Old Discord bot can still work during transition

### Phase 3: Verify Data Integrity

1. **Check Existing Sessions**
   ```sql
   SELECT id, xanax_count, xanax_stack, status FROM happy_jump_sessions;
   ```
   - Verify xanax_stack populated correctly
   - Existing sessions still functional

2. **Check Insurance Policies**
   ```sql
   SELECT policy_id, name, cost_type, coverage_type FROM insurance_policies;
   ```
   - Old fields preserved
   - New fields populated with defaults

3. **Check Raffles**
   ```sql
   SELECT raffle_id, max_tickets, tickets_available FROM raffles;
   ```
   - tickets_available = max_tickets
   - Both columns available

### Phase 4: Parallel Testing

Run both systems simultaneously:

1. **Old System**: Handles production traffic
2. **New System**: Available for admin testing

**Test Dashboard:**
- Login via OAuth
- Browse existing sessions
- Create test session
- Create test raffle
- Verify Discord posts work

**Test Discord Commands:**
- All existing commands work
- Bot still responds normally
- Users can interact as before

### Phase 5: Gradual Cutover

#### Week 1: Admin-Only Dashboard Access
- Announce dashboard to admins
- Train team on new features
- Create sessions via dashboard
- Monitor for issues

#### Week 2: Expanded Access
- Providers can use dashboard
- Create insurance policies via web
- Users still use Discord normally

#### Week 3: Full Cutover
- Announce new dashboard to community
- Update documentation links
- Point primary domain to vNext
- Monitor closely

#### Week 4+: Retire Old System
- Archive old Railway service
- Keep as backup for 30 days
- Final cleanup

### Phase 6: Cleanup

Once stable for 30 days:

1. **Remove Old System**
   ```bash
   # Railway dashboard
   # Delete old service
   ```

2. **Clean Old Columns** (optional)
   ```sql
   -- Only after 100% confident
   ALTER TABLE happy_jump_sessions DROP COLUMN IF EXISTS xanax_count;
   ALTER TABLE raffles DROP COLUMN IF EXISTS max_tickets;
   ALTER TABLE insurance_policies DROP COLUMN IF EXISTS premium_per_xanax;
   ```

## Step-by-Step Migration (Option B - In-Place)

⚠️ **Only use this if you cannot run parallel systems**

### 1. Pre-Migration Backup

```bash
# Supabase dashboard
# Database > Backups > Create Manual Backup
# Or use pg_dump:
pg_dump -h db.xxxxx.supabase.co -U postgres -d postgres > backup.sql
```

### 2. Announce Maintenance

Post to Discord:
```
🚨 MAINTENANCE WINDOW 🚨
Happy Jumper will be offline for 30 minutes starting at [TIME]
We're upgrading to vNext with new dashboard features!
Thank you for your patience.
```

### 3. Stop Old Service

```bash
# Railway dashboard
# [Old Service] > Settings > Pause
```

### 4. Deploy New Code

```bash
# Update repository with vNext code
git pull origin main
railway up
```

### 5. Monitor Migration

```bash
railway logs --follow
```

Watch for:
```
✓ Database pool initialized
✓ Applied 1 database migration (002_vnext_updates)
✓ Bot is ready!
✓ FastAPI web server started
```

### 6. Verify Everything Works

- [ ] Bot responds to `/ping`
- [ ] Dashboard loads at your URL
- [ ] Can login via Discord OAuth
- [ ] Existing sessions visible
- [ ] Can create new session
- [ ] Session posts to Discord correctly

### 7. Resume Service

Announce to Discord:
```
✅ MAINTENANCE COMPLETE ✅
Happy Jumper is back online with new features!
🌐 Dashboard now available at: https://your-app.railway.app
Check it out for an easier way to create sessions, raffles, and more!
```

## Data Migration (If Using Separate Database)

If you set up vNext with a completely new database:

### 1. Export Data from Old Database

```bash
# Export specific tables
pg_dump -h old-db -U postgres \
  -t user_api_keys \
  -t guild_settings \
  -t happy_jump_sessions \
  -t happy_jump_signups \
  -t insurance_providers \
  -t insurance_policies \
  -t raffles \
  > export.sql
```

### 2. Transform Data (if needed)

```sql
-- Update xanax_count to xanax_stack format
UPDATE happy_jump_sessions 
SET xanax_stack = CASE 
    WHEN xanax_count = 1 THEN '1_xanax'
    WHEN xanax_count = 2 THEN '2_xanax'
    WHEN xanax_count = 3 THEN '3_xanax'
    ELSE 'full_stack'
END;
```

### 3. Import to New Database

```bash
psql -h new-db -U postgres -d postgres < export.sql
```

## Rollback Procedure

If something goes wrong:

### Immediate Rollback (Option A)

```bash
# Revert DNS/domain to old service
# Users automatically redirected

# vNext stays running for debugging
# No data loss - shared database
```

### Emergency Rollback (Option B)

```bash
# Stop vNext service
railway pause

# Restore database backup
psql -h db -U postgres -d postgres < backup.sql

# Restart old service
railway up --service old-service
```

## Common Migration Issues

### Issue: Migrations Fail

**Symptoms:**
```
ERROR: column "xanax_stack" already exists
```

**Solution:**
```sql
-- Migration system tracks what's applied
SELECT * FROM schema_migrations;

-- If migration partially applied, fix manually:
-- Then mark as complete:
INSERT INTO schema_migrations (version, name) VALUES (2, 'vnext_updates');
```

### Issue: OAuth Not Working

**Symptoms:** Login fails or redirects to error page

**Solution:**
1. Verify redirect URI in Discord matches exactly
2. Check `DISCORD_CLIENT_SECRET` is correct
3. Ensure `DASHBOARD_URL` has no trailing slash

### Issue: Bot and Dashboard Conflicts

**Symptoms:** Bot responds but dashboard actions fail

**Solution:**
1. Ensure only ONE bot instance running
2. Check bot instance registered with admin API
3. Verify DATABASE_URL same for both

### Issue: Existing Sessions Not Showing

**Symptoms:** Dashboard shows no sessions, but Discord shows them

**Solution:**
1. Check guild_id filter in queries
2. Verify user has admin permissions
3. Check sessions table has announcement_message_id

## Performance Considerations

### Before Migration
- Note current response times
- Check database size
- Monitor memory usage

### After Migration
- Expect slight increase in memory (FastAPI + Frontend)
- Database queries should be similar or faster (new indexes)
- Bot latency unchanged (same core logic)

### Optimization Tips
1. **Add Indexes** (if not done):
   ```sql
   CREATE INDEX IF NOT EXISTS idx_sessions_created_at 
   ON happy_jump_sessions(created_at DESC);
   ```

2. **Monitor Slow Queries**:
   ```sql
   -- Enable pg_stat_statements in Supabase
   -- View slow queries in dashboard
   ```

3. **Scale if Needed**:
   - Railway: Upgrade to larger plan
   - Supabase: Upgrade database tier

## Feature Comparison

| Feature | Old System | vNext |
|---------|-----------|-------|
| Discord Commands | ✅ | ✅ |
| Web Dashboard | ❌ | ✅ |
| OAuth Login | ❌ | ✅ |
| Session Creation | Discord only | Discord + Web |
| Raffle Creation | Discord only | Discord + Web |
| Insurance Dashboard | ❌ | ✅ |
| Audit Logs | ❌ | ✅ |
| Migrations | ❌ | ✅ |
| Mobile Friendly | Discord app | Discord + Web app |

## Timeline Recommendations

**Small Server (<100 users):**
- Setup: 2 hours
- Testing: 1 day
- Migration: 1 hour
- Total: ~1 week

**Medium Server (100-1000 users):**
- Setup: 4 hours
- Testing: 1 week
- Migration: 2 hours
- Total: ~2 weeks

**Large Server (>1000 users):**
- Setup: 1 day
- Testing: 2 weeks
- Migration: 4 hours (parallel phase)
- Total: ~1 month

## Support During Migration

If you encounter issues:

1. **Check Logs:**
   ```bash
   railway logs --follow
   ```

2. **Database Status:**
   ```sql
   SELECT * FROM schema_migrations ORDER BY version;
   ```

3. **Rollback if Needed:**
   - Follow rollback procedure above
   - No shame in rolling back!
   - Debug offline, try again

4. **Community Help:**
   - Discord.py server
   - Railway community
   - GitHub issues

## Post-Migration Checklist

- [ ] All existing sessions visible and functional
- [ ] Dashboard accessible and responsive
- [ ] OAuth login works for all admins
- [ ] Can create sessions from dashboard
- [ ] Sessions post to Discord correctly
- [ ] Insurance policies working
- [ ] Raffles functional
- [ ] Audit log capturing events
- [ ] No errors in Railway logs
- [ ] Database backups configured
- [ ] Old system retired (if applicable)

---

**Questions?** Open an issue or reach out to the development team.

Good luck with your migration! 🚀
