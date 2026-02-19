# Docker Commands - RATPanel Server

Quick reference for managing RATPanel server with Docker Compose.

---

## Quick Commands

### Update Code from Git
```bash
cd /opt/server && git pull origin main && docker compose -f docker-compose.prod.yml build --no-cache api && docker compose -f docker-compose.prod.yml up -d --force-recreate api
```

### Restart API
```bash
docker compose -f docker-compose.prod.yml restart api
```

### View Logs (Real-time)
```bash
docker compose -f docker-compose.prod.yml logs -f api
```

### Stop All
```bash
docker compose -f docker-compose.prod.yml stop
```

### Start All
```bash
docker compose -f docker-compose.prod.yml up -d
```

---

## 1. Update Code

### Method 1: Git Pull + Rebuild
```bash
cd /opt/server
git pull origin main
docker compose -f docker-compose.prod.yml build --no-cache api
docker compose -f docker-compose.prod.yml up -d --force-recreate api
docker compose -f docker-compose.prod.yml logs -f api
```

### Method 2: Manual File Copy
```bash
# From local machine
scp -r server/* root@YOUR_SERVER_IP:/opt/server/

# On server
cd /opt/server
docker compose -f docker-compose.prod.yml build --no-cache api
docker compose -f docker-compose.prod.yml up -d api
```

### Method 3: Single File Update (No Rebuild)
```bash
# Copy file
scp server/app/main.py root@YOUR_SERVER_IP:/opt/server/server/app/

# Restart only
docker compose -f docker-compose.prod.yml restart api
```

---

## 2. First Time Setup

```bash
# Clone project
git clone https://github.com/zaramoklo-spec/server.git /opt/server
cd /opt/server

# Copy secret files (admin.json, apps.json) to server/ directory

# Build and start
docker compose -f docker-compose.prod.yml up -d --build

# Check status
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f
```

---

## 3. Start / Stop / Restart

### Stop
```bash
docker compose -f docker-compose.prod.yml stop
docker compose -f docker-compose.prod.yml stop api
docker compose -f docker-compose.prod.yml stop mongodb
docker compose -f docker-compose.prod.yml stop redis
```

### Start
```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml up -d api
```

### Restart
```bash
docker compose -f docker-compose.prod.yml restart
docker compose -f docker-compose.prod.yml restart api
docker compose -f docker-compose.prod.yml restart mongodb
docker compose -f docker-compose.prod.yml restart redis
```

---

## 4. Rebuild

### Rebuild API
```bash
docker compose -f docker-compose.prod.yml build --no-cache api
docker compose -f docker-compose.prod.yml up -d --force-recreate api
```

### Rebuild All
```bash
docker compose -f docker-compose.prod.yml build --no-cache
docker compose -f docker-compose.prod.yml up -d --force-recreate
```

---

## 5. View Logs

### Real-time Logs
```bash
docker compose -f docker-compose.prod.yml logs -f
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f mongodb
docker compose -f docker-compose.prod.yml logs -f redis
```

### Last 1000 Lines
```bash
docker compose -f docker-compose.prod.yml logs --tail=1000 api
```

### Last 1 Hour
```bash
docker compose -f docker-compose.prod.yml logs --since 1h api
```

### Save to File
```bash
docker compose -f docker-compose.prod.yml logs api > api_logs.txt
docker logs RATPanel_api > api_logs_complete.txt 2>&1
```

### Save with Timestamp
```bash
docker compose -f docker-compose.prod.yml logs --timestamps api > api_logs.txt
```

### Compress Logs
```bash
docker logs RATPanel_api 2>&1 | gzip > api_logs_$(date +%Y%m%d_%H%M%S).gz
```

### Search Errors
```bash
docker compose -f docker-compose.prod.yml logs api | grep -i "error"
docker compose -f docker-compose.prod.yml logs api | grep -i "exception"
```

---

## 6. Monitor Ping Task

### Real-time Ping Logs
```bash
docker compose -f docker-compose.prod.yml logs -f api | grep --line-buffered "Ping Task"
```

### Check Last Pings
```bash
docker compose -f docker-compose.prod.yml logs --tail=2000 api | grep "Ping Task" | tail -50
```

### Clear Background Locks
```bash
curl -X POST http://localhost/api/admin/clear-background-locks -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 7. Backup & Restore

### Backup MongoDB
```bash
docker compose -f docker-compose.prod.yml exec mongodb mongodump --uri="mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --out=/data/backup/$(date +%Y%m%d_%H%M%S)
docker cp RATPanel_mongodb:/data/backup ./mongodb_backup
```

### Backup Compressed
```bash
docker compose -f docker-compose.prod.yml exec mongodb sh -c "mongodump --uri='mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin' --archive | gzip > /data/backup/backup_$(date +%Y%m%d_%H%M%S).gz"
docker cp RATPanel_mongodb:/data/backup/backup_20240219_120000.gz ./
```

### Restore MongoDB
```bash
docker cp backup_20240219_120000.gz RATPanel_mongodb:/tmp/
docker compose -f docker-compose.prod.yml exec mongodb sh -c "gunzip < /tmp/backup_20240219_120000.gz | mongorestore --uri='mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin' --archive --drop"
```

### Auto Backup Script
```bash
cat > /opt/server/backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/mongodb"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR
cd /opt/server
docker compose -f docker-compose.prod.yml exec -T mongodb mongodump --uri="mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --archive | gzip > $BACKUP_DIR/backup_$DATE.gz
find $BACKUP_DIR -name "backup_*.gz" -mtime +7 -delete
echo "Backup completed: $BACKUP_DIR/backup_$DATE.gz"
EOF

chmod +x /opt/server/backup.sh
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/server/backup.sh >> /var/log/mongodb-backup.log 2>&1") | crontab -
```

---

## 8. MongoDB Shell

### Connect to MongoDB
```bash
docker compose -f docker-compose.prod.yml exec mongodb mongosh -u owner -p 'A123456789@' --authenticationDatabase admin
```

### Run Query
```bash
docker compose -f docker-compose.prod.yml exec mongodb mongosh "mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --eval "db.devices.countDocuments()"
```

### Export Collection
```bash
docker compose -f docker-compose.prod.yml exec mongodb mongoexport --uri="mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --collection=devices --out=/tmp/devices.json
docker cp RATPanel_mongodb:/tmp/devices.json ./
```

### Import Collection
```bash
docker cp devices.json RATPanel_mongodb:/tmp/
docker compose -f docker-compose.prod.yml exec mongodb mongoimport --uri="mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --collection=devices --file=/tmp/devices.json
```

---

## 9. Redis CLI

### Connect to Redis
```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli
```

### Check Keys
```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli KEYS "device:*"
```

### Count Keys
```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli DBSIZE
```

### Flush All (WARNING: Deletes all data!)
```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli FLUSHALL
```

---

## 10. Monitoring

### Resource Usage (Real-time)
```bash
docker stats
docker stats RATPanel_api RATPanel_mongodb RATPanel_redis
```

### Container Status
```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml ps -a
```

### Health Check
```bash
docker inspect RATPanel_api --format='{{.State.Health.Status}}'
curl http://localhost/health
```

### Disk Usage
```bash
docker system df
docker system df -v
df -h
```

---

## 11. Cleanup

### Remove Containers (Keep data)
```bash
docker compose -f docker-compose.prod.yml down
```

### Remove Containers + Volumes (WARNING: Deletes MongoDB data!)
```bash
docker compose -f docker-compose.prod.yml down -v
```

### Clean Old Images
```bash
docker system prune -a
```

### Clean Volumes
```bash
docker volume prune
```

### Truncate Logs
```bash
sudo truncate -s 0 $(docker inspect --format='{{.LogPath}}' RATPanel_api)
```

---

## 12. Troubleshooting

### Container Won't Start
```bash
docker compose -f docker-compose.prod.yml logs api
docker compose -f docker-compose.prod.yml ps -a
docker inspect RATPanel_api --format='{{.State.ExitCode}}'
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml build --no-cache api
docker compose -f docker-compose.prod.yml up -d api
```

### MongoDB Connection Error
```bash
docker compose -f docker-compose.prod.yml ps mongodb
docker compose -f docker-compose.prod.yml logs mongodb
docker compose -f docker-compose.prod.yml exec mongodb mongosh -u owner -p 'A123456789@' --authenticationDatabase admin --eval "db.adminCommand('ping')"
docker compose -f docker-compose.prod.yml restart mongodb
```

### Redis Connection Error
```bash
docker compose -f docker-compose.prod.yml ps redis
docker compose -f docker-compose.prod.yml logs redis
docker compose -f docker-compose.prod.yml exec redis redis-cli PING
docker compose -f docker-compose.prod.yml restart redis
```

### Slow API Response
```bash
docker stats RATPanel_api RATPanel_mongodb RATPanel_redis
docker compose -f docker-compose.prod.yml exec mongodb mongosh -u owner -p 'A123456789@' --authenticationDatabase admin --eval "db.serverStatus().connections"
docker compose -f docker-compose.prod.yml restart api
```

### Port Already in Use
```bash
sudo lsof -i :80
sudo kill -9 <PID>
```

### Disk Space Full
```bash
df -h
docker system df
docker system prune -a
docker volume prune
sudo truncate -s 0 $(docker inspect --format='{{.LogPath}}' RATPanel_api)
```

### Ping Task Not Working
```bash
docker compose -f docker-compose.prod.yml logs api | grep "Ping Task"
docker compose -f docker-compose.prod.yml exec mongodb mongosh -u owner -p 'A123456789@' --authenticationDatabase admin --eval "db.background_locks.find().pretty()"
curl -X POST http://localhost/api/admin/clear-background-locks -H "Authorization: Bearer YOUR_TOKEN"
docker compose -f docker-compose.prod.yml restart api
```

---

## One-Liner Commands

### Full Reset
```bash
docker compose -f docker-compose.prod.yml down -v && docker compose -f docker-compose.prod.yml build --no-cache && docker compose -f docker-compose.prod.yml up -d
```

### Quick Restart
```bash
docker compose -f docker-compose.prod.yml restart && docker compose -f docker-compose.prod.yml logs -f
```

### Rebuild API Only
```bash
docker compose -f docker-compose.prod.yml build --no-cache api && docker compose -f docker-compose.prod.yml up -d --force-recreate api && docker compose -f docker-compose.prod.yml logs -f api
```

### Full Update (Git + Rebuild)
```bash
cd /opt/server && git pull origin main && docker compose -f docker-compose.prod.yml build --no-cache && docker compose -f docker-compose.prod.yml up -d --force-recreate && docker compose -f docker-compose.prod.yml logs -f
```

### Backup + Restart
```bash
docker compose -f docker-compose.prod.yml exec -T mongodb mongodump --uri="mongodb://owner:A123456789%40@localhost:27017/RATPanel?authSource=admin" --archive | gzip > backup_$(date +%Y%m%d_%H%M%S).gz && docker compose -f docker-compose.prod.yml restart
```

### Search Errors in Logs
```bash
docker compose -f docker-compose.prod.yml logs api | grep -i "error\|exception\|failed"
```

### Check Everything
```bash
docker compose -f docker-compose.prod.yml ps && docker stats --no-stream && df -h
```

---

## Important Notes

- `down -v` deletes MongoDB data - always backup first
- `--no-cache` forces complete rebuild (slower but safer)
- `-d` runs containers in background (detached mode)
- `-f` follows logs in real-time
- Port 80 must be free (stop nginx/apache if needed)
- Keep admin.json and apps.json out of git
- Setup regular backups with cron
- Monitor disk space regularly

---

## Links

- Server: https://github.com/zaramoklo-spec/server
- Panel: https://github.com/zaramoklo-spec/panel
- MyApp: https://github.com/zaramoklo-spec/myapp

---

Last Updated: 2024-02-19
