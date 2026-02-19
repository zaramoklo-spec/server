# Update Guide - How to Update Source Code and Restart Server

Simple guide for updating source code and restarting the server.

---

## Quick Start

### Development Mode (docker-compose.yml)

**Method 1: Automatic Reload (Recommended)**
```bash
cd server
# Just update your code files
# Server will automatically reload (because --reload flag is enabled)
# No need to restart!
```

**Method 2: Manual Restart**
```bash
cd server
# Update your code
git pull  # or copy new files

# Restart server only
docker-compose restart server

# Or restart server and worker
docker-compose restart server fcm_ping_worker_v2
```

### Production Mode (docker-compose.prod.yml)

```bash
cd server
# 1. Stop services
docker-compose -f docker-compose.prod.yml stop api fcm_ping_worker_v2

# 2. Update source code
git pull  # or copy new files

# 3. Rebuild containers
docker-compose -f docker-compose.prod.yml build --no-cache api fcm_ping_worker_v2

# 4. Start services
docker-compose -f docker-compose.prod.yml up -d api fcm_ping_worker_v2

# 5. Check logs
docker-compose -f docker-compose.prod.yml logs -f api
```

---

## Detailed Steps

### Development Mode Update

**Step 1: Update Source Code**
```bash
cd server
git pull
# Or manually copy updated files to server/app/ directory
```

**Step 2: Restart Services**

**Option A: Restart Server Only**
```bash
docker-compose restart server
```

**Option B: Restart Server and Worker**
```bash
docker-compose restart server fcm_ping_worker_v2
```

**Option C: Full Restart (All Services)**
```bash
docker-compose restart
```

**Step 3: Check Logs**
```bash
# Check if server started successfully
docker-compose logs -f server

# Check worker logs
docker-compose logs -f fcm_ping_worker_v2
```

**Note:** In Development mode, if you have `--reload` enabled (which is default), changes in `./app` directory are automatically detected and server reloads without restart!

---

### Production Mode Update

**Step 1: Stop Services**
```bash
cd server
docker-compose -f docker-compose.prod.yml stop api fcm_ping_worker_v2
```

**Step 2: Update Source Code**
```bash
git pull
# Or manually copy updated files
```

**Step 3: Rebuild Containers**
```bash
# Rebuild without cache (ensures fresh build)
docker-compose -f docker-compose.prod.yml build --no-cache api fcm_ping_worker_v2
```

**Step 4: Start Services**
```bash
docker-compose -f docker-compose.prod.yml up -d api fcm_ping_worker_v2
```

**Step 5: Verify**
```bash
# Check container status
docker-compose -f docker-compose.prod.yml ps

# Check logs
docker-compose -f docker-compose.prod.yml logs -f api

# Check health
docker inspect RATPanel_api | grep -A 10 Health
```

---

## Update Scenarios

### Scenario 1: Only Python Code Changed (app/*.py)

**Development:**
```bash
# No restart needed! Auto-reload will handle it
# Or if you want to be sure:
docker-compose restart server
```

**Production:**
```bash
docker-compose -f docker-compose.prod.yml stop api
docker-compose -f docker-compose.prod.yml build --no-cache api
docker-compose -f docker-compose.prod.yml up -d api
```

### Scenario 2: Dependencies Changed (requirements.txt)

**Both Development and Production:**
```bash
# Must rebuild to install new packages
docker-compose build --no-cache server
docker-compose up -d server
```

### Scenario 3: Configuration Changed (.env or config.py)

**Development:**
```bash
# Restart to load new config
docker-compose restart server fcm_ping_worker_v2
```

**Production:**
```bash
# Rebuild and restart
docker-compose -f docker-compose.prod.yml stop api fcm_ping_worker_v2
docker-compose -f docker-compose.prod.yml build --no-cache api fcm_ping_worker_v2
docker-compose -f docker-compose.prod.yml up -d api fcm_ping_worker_v2
```

### Scenario 4: Scripts Changed (scripts/*.py)

**Development:**
```bash
# Restart worker
docker-compose restart fcm_ping_worker_v2
```

**Production:**
```bash
docker-compose -f docker-compose.prod.yml stop fcm_ping_worker_v2
docker-compose -f docker-compose.prod.yml build --no-cache fcm_ping_worker_v2
docker-compose -f docker-compose.prod.yml up -d fcm_ping_worker_v2
```

---

## One-Line Commands

### Development Mode

```bash
# Quick restart server
cd server && docker-compose restart server

# Quick restart all
cd server && docker-compose restart

# Update and restart
cd server && git pull && docker-compose restart server fcm_ping_worker_v2
```

### Production Mode

```bash
# Full update and restart
cd server && git pull && docker-compose -f docker-compose.prod.yml stop api fcm_ping_worker_v2 && docker-compose -f docker-compose.prod.yml build --no-cache api fcm_ping_worker_v2 && docker-compose -f docker-compose.prod.yml up -d api fcm_ping_worker_v2
```

---

## Verify Update Success

### Check Server Status
```bash
# Development
docker-compose ps

# Production
docker-compose -f docker-compose.prod.yml ps
```

### Check Logs for Errors
```bash
# Development
docker-compose logs --tail=50 server | grep -i error

# Production
docker-compose -f docker-compose.prod.yml logs --tail=50 api | grep -i error
```

### Test Health Endpoint
```bash
# Test if server is responding
curl http://localhost/health

# Or in browser
# http://localhost/health
```

### Check Application Logs
```bash
# Real-time logs
docker-compose logs -f server

# Last 100 lines
docker-compose logs --tail=100 server
```

---

## Troubleshooting

### Server Won't Start After Update

```bash
# 1. Check logs
docker-compose logs server

# 2. Check for syntax errors
docker exec RATPanel_server python -m py_compile app/main.py

# 3. Rebuild from scratch
docker-compose build --no-cache server
docker-compose up -d server
```

### Changes Not Applied

**Development:**
```bash
# Force restart
docker-compose restart server

# Or check if volume mount is working
docker exec RATPanel_server ls -la /app/app
```

**Production:**
```bash
# Make sure you rebuilt
docker-compose -f docker-compose.prod.yml build --no-cache api
docker-compose -f docker-compose.prod.yml up -d api
```

### Worker Not Updating

```bash
# Restart worker
docker-compose restart fcm_ping_worker_v2

# Check worker logs
docker-compose logs -f fcm_ping_worker_v2
```

---

## Best Practices

1. **Always check logs after update**
   ```bash
   docker-compose logs -f server
   ```

2. **Test in Development first**
   - Update Development environment
   - Test thoroughly
   - Then update Production

3. **Backup before major updates**
   ```bash
   docker exec test mongodump --uri="mongodb://ownerapps:9kiwlPwQ2ICpc1F@localhost:27017/RATPanel?authSource=admin" --out=/data/backup
   ```

4. **Update during low traffic hours** (for Production)

5. **Monitor after update**
   - Check logs for 5-10 minutes
   - Monitor error rates
   - Test critical endpoints

---

## Quick Reference

| Action | Development Command | Production Command |
|--------|-------------------|-------------------|
| Restart server | `docker-compose restart server` | `docker-compose -f docker-compose.prod.yml restart api` |
| Restart worker | `docker-compose restart fcm_ping_worker_v2` | `docker-compose -f docker-compose.prod.yml restart fcm_ping_worker_v2` |
| Rebuild | `docker-compose build --no-cache server` | `docker-compose -f docker-compose.prod.yml build --no-cache api` |
| View logs | `docker-compose logs -f server` | `docker-compose -f docker-compose.prod.yml logs -f api` |
| Check status | `docker-compose ps` | `docker-compose -f docker-compose.prod.yml ps` |

---

## Summary

**Development Mode:**
- Usually just `docker-compose restart server` is enough
- Auto-reload handles most code changes
- Volume mount makes changes instant

**Production Mode:**
- Always rebuild: `build --no-cache`
- Then restart: `up -d`
- Check logs to verify

---

**Last Updated:** 2024-12-24





