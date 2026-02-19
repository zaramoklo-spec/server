# How to View Logs - Docker Commands

Quick reference for viewing server logs in Docker.

---

## View Server Logs

### Development Mode (docker-compose.yml)
```bash
# Server only
docker compose logs -f server

# Worker only
docker compose logs -f fcm_ping_worker_v2

# All services
docker compose logs -f
```

### Production Mode (docker-compose.prod.yml)
```bash
# API server - Only NEW logs from now (no old logs)
docker compose -f docker-compose.prod.yml logs -f --tail=0 api

# API server - Only PING logs from now
docker compose -f docker-compose.prod.yml logs -f --tail=0 api | grep -i "PING"

# Worker - Only new logs
docker compose -f docker-compose.prod.yml logs -f --tail=0 fcm_ping_worker_v2

# Worker - Only failed/uninstalled devices
docker compose -f docker-compose.prod.yml logs -f --tail=0 fcm_ping_worker_v2 | grep -i "failed\|uninstalled\|WORKER.*Device.*marked"

# All services - Only new logs
docker compose -f docker-compose.prod.yml logs -f --tail=0
```

### Last N Lines
```bash
# Development - Last 100 lines
docker compose logs --tail=100 server

# Production - Last 100 lines
docker compose -f docker-compose.prod.yml logs --tail=100 api
```

### Filter Logs
```bash
# Development - Filter by "PING"
docker compose logs server | grep -i "PING"

# Production - Filter by "PING"
docker compose -f docker-compose.prod.yml logs api | grep -i "PING"

# Filter by device ID
docker compose -f docker-compose.prod.yml logs api | grep "DEVICE_ID_HERE"

# Filter errors only
docker compose -f docker-compose.prod.yml logs api | grep -i "error"
```

### Save Logs to File
```bash
# Production - Save all logs
docker compose -f docker-compose.prod.yml logs api > server_logs.txt

# Production - Save filtered logs
docker compose -f docker-compose.prod.yml logs api | grep -i "PING" > ping_logs.txt
```

---

## Quick Commands (Production)

```bash
# Watch ONLY NEW ping logs (from now, no old logs)
docker compose -f docker-compose.prod.yml logs -f --tail=0 api | grep -i "PING"

# Watch ONLY NEW logs (from now, no old logs)
docker compose -f docker-compose.prod.yml logs -f --tail=0 api

# Check old logs (if needed)
docker compose -f docker-compose.prod.yml logs --tail=200 api | grep -i "PING"
```

## Debug Device Marking/Deletion Issues

```bash
# Watch device marking/uninstalling logs (API)
docker compose -f docker-compose.prod.yml logs -f --tail=0 api | grep -i "MARK_UNINSTALLED\|PING.*Device status"

# Watch device marking/uninstalling logs (Worker)
docker compose -f docker-compose.prod.yml logs -f --tail=0 fcm_ping_worker_v2 | grep -i "UNINSTALLED\|WORKER.*Device.*marked\|failed"

# Watch for device deletion (should NOT happen during ping)
docker compose -f docker-compose.prod.yml logs -f --tail=0 api | grep -i "delete\|DELETED"

# Watch all device status changes (API + Worker)
docker compose -f docker-compose.prod.yml logs -f --tail=0 | grep -E "Device status|is_deleted|is_uninstalled|UNINSTALLED"

# Check specific device (all services)
docker compose -f docker-compose.prod.yml logs | grep "DEVICE_ID_HERE"

# Watch worker failed pings
docker compose -f docker-compose.prod.yml logs -f --tail=0 fcm_ping_worker_v2 | grep -E "failed|UNINSTALLED|⚠️"
```

---

**Tip:** Use `Ctrl+C` to stop watching logs.

