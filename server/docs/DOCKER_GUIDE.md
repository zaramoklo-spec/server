# Docker Guide - RATPanel Server

Complete guide for working with Docker for RATPanel Server.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Initial Setup](#initial-setup)
3. [Development Mode](#development-mode)
4. [Production Mode](#production-mode)
5. [Viewing Logs](#viewing-logs)
6. [Updating Source Code](#updating-source-code)
7. [Container Management](#container-management)
8. [Backup and Restore](#backup-and-restore)
9. [Environment Configuration](#environment-configuration)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Install Docker and Docker Compose

**Windows:**
- Download and install Docker Desktop from: https://www.docker.com/products/docker-desktop

**Linux (Ubuntu/Debian):**
```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version
```

### Check Access
```bash
docker ps
```

---

## Initial Setup

### 1. Configure .env File

```bash
cd server
cp .env.example .env
# Edit .env file and enter real values
```

**Important values in .env:**
- `SECRET_KEY`: Security key (minimum 32 characters)
- `MONGODB_URL`: MongoDB address
- `REDIS_URL`: Redis address
- `TELEGRAM_BOT_TOKEN`: Telegram bot tokens
- Other settings...

### 2. Check Firebase Files

Make sure these files exist:
- `apps.json` - For Firebase Admin SDK
- `admin.json` - For Firebase Admin (optional)

---

## Development Mode

### Start All Services

```bash
cd server
docker-compose up -d
```

**Running Services:**
- `RATPanel_server` - Main server (port 80)
- `RATPanel_redis` - Redis (port 6379)
- `test` - MongoDB (port 27017)
- `RATPanel_fcm_worker_v2` - FCM Ping Worker

### View Live Logs

```bash
# All services logs
docker-compose logs -f

# Server logs only
docker-compose logs -f server

# MongoDB logs only
docker-compose logs -f mongodb

# Redis logs only
docker-compose logs -f redis

# Worker logs only
docker-compose logs -f fcm_ping_worker_v2
```

### Stop Services

```bash
# Stop without removing
docker-compose stop

# Stop and remove containers
docker-compose down

# Stop and remove containers + volumes (WARNING - data will be deleted)
docker-compose down -v
```

### Restart Services

```bash
# Restart all services
docker-compose restart

# Restart server only
docker-compose restart server

# Restart worker only
docker-compose restart fcm_ping_worker_v2
```

---

## Production Mode

### Using docker-compose.prod.yml

```bash
# Run production
docker-compose -f docker-compose.prod.yml up -d

# View logs
docker-compose -f docker-compose.prod.yml logs -f

# Stop
docker-compose -f docker-compose.prod.yml down
```

**Differences from Development:**
- Uses Gunicorn instead of direct Uvicorn
- Health checks for all services
- Better optimized settings for MongoDB and Redis
- No auto-reload (for better stability)

---

## Viewing Logs

### Real-time Logs

```bash
# All logs
docker-compose logs -f

# Server logs (last 100 lines)
docker-compose logs --tail=100 -f server

# Worker logs
docker-compose logs -f fcm_ping_worker_v2

# MongoDB logs
docker-compose logs -f mongodb

# Redis logs
docker-compose logs -f redis
```

### Saved Logs

```bash
# View older logs
docker-compose logs --tail=500 server

# Save logs to file
docker-compose logs server > server_logs.txt

# Logs from specific time
docker-compose logs --since 2024-01-01T00:00:00 server
```

### Logs Inside Container

```bash
# Enter server container
docker exec -it RATPanel_server bash

# View logs inside container
docker exec RATPanel_server tail -f /var/log/app.log  # if log file exists
```

---

## Updating Source Code

### Method 1: Update with Rebuild

```bash
# 1. Stop services
docker-compose stop server fcm_ping_worker_v2

# 2. Pull latest changes (if using Git)
git pull

# 3. Rebuild and restart
docker-compose build --no-cache server fcm_ping_worker_v2
docker-compose up -d server fcm_ping_worker_v2

# 4. Check logs
docker-compose logs -f server
```

### Method 2: Quick Update (without Rebuild)

```bash
# 1. Stop services
docker-compose stop server fcm_ping_worker_v2

# 2. Pull latest changes
git pull

# 3. Restart (with volume mount, changes apply immediately)
docker-compose up -d server fcm_ping_worker_v2

# 4. Check logs
docker-compose logs -f server
```

### Method 3: Production Update

```bash
# 1. Stop services
docker-compose -f docker-compose.prod.yml stop api fcm_ping_worker_v2

# 2. Pull latest changes
git pull

# 3. Rebuild
docker-compose -f docker-compose.prod.yml build --no-cache api fcm_ping_worker_v2

# 4. Restart
docker-compose -f docker-compose.prod.yml up -d api fcm_ping_worker_v2

# 5. Check health
docker-compose -f docker-compose.prod.yml ps
```

### Update Dependencies Only

```bash
# 1. Edit requirements.txt (if needed)

# 2. Rebuild
docker-compose build --no-cache server

# 3. Restart
docker-compose up -d server
```

---

## Container Management

### View Container Status

```bash
# List all containers
docker-compose ps

# View details
docker-compose ps -a

# View resource usage
docker stats
```

### Enter Container

```bash
# Enter server container
docker exec -it RATPanel_server bash

# Enter MongoDB container
docker exec -it test mongosh -u ownerapps -p 9kiwlPwQ2ICpc1F --authenticationDatabase admin

# Enter Redis container
docker exec -it RATPanel_redis redis-cli
```

### Run Commands in Container

```bash
# Run Python script
docker exec RATPanel_server python scripts/create_indexes.py

# Run command in worker
docker exec RATPanel_fcm_worker_v2 python -c "print('test')"

# View environment variables
docker exec RATPanel_server env
```

### Remove and Recreate Container

```bash
# Remove container (without removing volume)
docker-compose rm -f server

# Recreate
docker-compose up -d server

# Remove and recreate with rebuild
docker-compose up -d --force-recreate --build server
```

---

## Backup and Restore

### Backup MongoDB

```bash
# Full MongoDB backup
docker exec test mongodump --uri="mongodb://ownerapps:9kiwlPwQ2ICpc1F@localhost:27017/RATPanel?authSource=admin" --out=/data/backup

# Copy backup to host
docker cp test:/data/backup ./backups/mongodb_backup_$(date +%Y%m%d_%H%M%S)

# Or use script
docker exec RATPanel_server python scripts/db_backup_restore.py --backup
```

### Restore MongoDB

```bash
# Restore from backup
docker exec -i test mongorestore --uri="mongodb://ownerapps:9kiwlPwQ2ICpc1F@localhost:27017/RATPanel?authSource=admin" --drop /data/backup/RATPanel

# Or from host
docker cp ./backups/mongodb_backup_20240101_120000 test:/data/backup
docker exec test mongorestore --uri="mongodb://ownerapps:9kiwlPwQ2ICpc1F@localhost:27017/RATPanel?authSource=admin" /data/backup/RATPanel
```

### Backup Redis

```bash
# Backup Redis (AOF file)
docker exec RATPanel_redis redis-cli BGSAVE
docker cp RATPanel_redis:/data/appendonly.aof ./backups/redis_backup_$(date +%Y%m%d_%H%M%S).aof
```

### Backup Volumes

```bash
# Backup MongoDB volume
docker run --rm -v server_mongodb_data:/data -v $(pwd)/backups:/backup ubuntu tar czf /backup/mongodb_volume_$(date +%Y%m%d_%H%M%S).tar.gz /data

# Backup Redis volume
docker run --rm -v server_redis_data:/data -v $(pwd)/backups:/backup ubuntu tar czf /backup/redis_volume_$(date +%Y%m%d_%H%M%S).tar.gz /data
```

---

## Environment Configuration

### Change Ports

Edit `docker-compose.yml`:
```yaml
services:
  server:
    ports:
      - "8080:80"  # Change external port to 8080
```

Then:
```bash
docker-compose up -d
```

### Change Environment Variables

**Method 1: Edit docker-compose.yml**
```yaml
services:
  server:
    environment:
      DEBUG: "False"
      SERVER_PORT: "80"
```

**Method 2: Use .env**
```bash
# Edit .env file
DEBUG=False
SERVER_PORT=80
```

Then:
```bash
docker-compose up -d
```

### Set Resource Limits

Edit `docker-compose.yml`:
```yaml
services:
  server:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '1'
          memory: 1G
```

---

## Troubleshooting

### Check Container Status

```bash
# View status
docker-compose ps

# Check health check
docker inspect RATPanel_server | grep -A 10 Health

# View events
docker events
```

### Check Error Logs

```bash
# Error logs only
docker-compose logs server | grep -i error

# Warning logs
docker-compose logs server | grep -i warning

# Logs from last hour
docker-compose logs --since 1h server
```

### Check Database Connection

```bash
# Test MongoDB connection
docker exec RATPanel_server python -c "from app.database import mongodb; import asyncio; asyncio.run(mongodb.client.admin.command('ping'))"

# Test Redis connection
docker exec RATPanel_redis redis-cli ping
```

### Check Network

```bash
# View networks
docker network ls

# Inspect specific network
docker network inspect server_RATPanel_network

# Test connection between containers
docker exec RATPanel_server ping mongodb
docker exec RATPanel_server ping redis
```

### Check Resource Usage

```bash
# View CPU and Memory usage
docker stats

# View disk usage
docker system df

# View volumes
docker volume ls
docker volume inspect server_mongodb_data
```

### Common Issues

**1. Container won't start:**
```bash
# Check logs
docker-compose logs server

# Check port in use
netstat -an | grep :80  # Windows
lsof -i :80  # Linux/Mac

# Check volumes
docker volume ls
```

**2. Can't connect to database:**
```bash
# Check MongoDB status
docker-compose logs mongodb

# Test manual connection
docker exec -it test mongosh -u ownerapps -p 9kiwlPwQ2ICpc1F --authenticationDatabase admin
```

**3. Worker not working:**
```bash
# Check worker logs
docker-compose logs fcm_ping_worker_v2

# Check Redis
docker exec RATPanel_redis redis-cli ping

# Restart worker
docker-compose restart fcm_ping_worker_v2
```

**4. Changes not applying:**
```bash
# Full rebuild
docker-compose build --no-cache server
docker-compose up -d server

# Or remove and recreate
docker-compose rm -f server
docker-compose up -d --build server
```

---

## Useful Commands

### Cleanup

```bash
# Remove stopped containers
docker-compose rm -f

# Clean unused images
docker image prune -a

# Full cleanup (WARNING - dangerous)
docker system prune -a --volumes
```

### View Information

```bash
# View Docker Compose version
docker-compose version

# View configuration
docker-compose config

# View environment variables
docker-compose exec server env
```

### Run Scripts

```bash
# Run create indexes script
docker exec RATPanel_server python scripts/create_indexes.py

# Run backup script
docker exec RATPanel_server python scripts/db_backup_restore.py --backup

# Run ping inactive devices script
docker exec RATPanel_server python scripts/ping_inactive_devices.py
```

---

## Important Notes

1. **Always backup before important changes**
2. **Use `docker-compose.prod.yml` in production**
3. **Regularly check logs**
4. **Set resource limits based on needs**
5. **Keep health checks enabled**
6. **Never commit `.env` file**

---

## Support

If you encounter problems:
1. Check logs
2. Check container status
3. Test database and Redis connections
4. Use troubleshooting commands

---

**Last Updated:** 2024-12-24
