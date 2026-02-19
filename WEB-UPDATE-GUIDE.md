# Flutter Web Panel - Update Guide

Complete guide for updating and managing the Flutter web panel on server.

---

## Quick Update

### One-Liner Command
```bash
sudo systemctl stop ratpanel-web && sudo rm -rf /opt/web/* && sudo cp -r /path/to/new/build/web/* /opt/web/ && sudo systemctl start ratpanel-web
```

---

## Step-by-Step Update

### Step 1: Build Flutter Web (On Local Machine)
```bash
cd /path/to/paneladmin
flutter clean
flutter pub get
flutter build web --release
```

### Step 2: Upload to Server
```bash
# Method 1: Using SCP
scp -r build/web/* root@YOUR_SERVER_IP:/tmp/web-temp/

# Method 2: Using rsync (faster for updates)
rsync -avz --delete build/web/ root@YOUR_SERVER_IP:/tmp/web-temp/

# Method 3: Using Git (if server has git access)
# Push to GitHub, then pull on server
git add .
git commit -m "Update web panel"
git push origin main
```

### Step 3: Stop Service (On Server)
```bash
sudo systemctl stop ratpanel-web
```

### Step 4: Replace Files (On Server)
```bash
# Remove old files
sudo rm -rf /opt/web/*

# Copy new files
sudo cp -r /tmp/web-temp/* /opt/web/

# Set permissions
sudo chown -R www-data:www-data /opt/web/
sudo chmod -R 755 /opt/web/
```

### Step 5: Start Service (On Server)
```bash
sudo systemctl start ratpanel-web
```

### Step 6: Verify
```bash
# Check service status
sudo systemctl status ratpanel-web

# Check logs
sudo journalctl -u ratpanel-web -n 50

# Test access
curl http://localhost:5000
```

---

## Update via Git (Recommended)

### On Local Machine
```bash
cd /path/to/paneladmin
flutter build web --release
git add .
git commit -m "Update web panel build"
git push origin main
```

### On Server
```bash
cd /opt/paneladmin
git pull origin main
sudo systemctl stop ratpanel-web
sudo rm -rf /opt/web/*
sudo cp -r build/web/* /opt/web/
sudo systemctl start ratpanel-web
sudo systemctl status ratpanel-web
```

---

## Service Management

### Start Service
```bash
sudo systemctl start ratpanel-web
```

### Stop Service
```bash
sudo systemctl stop ratpanel-web
```

### Restart Service
```bash
sudo systemctl restart ratpanel-web
```

### Check Status
```bash
sudo systemctl status ratpanel-web
```

### Enable Auto-start (on boot)
```bash
sudo systemctl enable ratpanel-web
```

### Disable Auto-start
```bash
sudo systemctl disable ratpanel-web
```

---

## View Logs

### Real-time Logs
```bash
sudo journalctl -u ratpanel-web -f
```

### Last 100 Lines
```bash
sudo journalctl -u ratpanel-web -n 100
```

### Last 1 Hour
```bash
sudo journalctl -u ratpanel-web --since "1 hour ago"
```

### Today's Logs
```bash
sudo journalctl -u ratpanel-web --since today
```

### Save Logs to File
```bash
sudo journalctl -u ratpanel-web > ratpanel-web-logs.txt
```

### Search for Errors
```bash
sudo journalctl -u ratpanel-web | grep -i error
```

---

## First Time Setup

### Install Python HTTP Server
```bash
sudo apt update
sudo apt install python3 -y
```

### Create Web Directory
```bash
sudo mkdir -p /opt/web
sudo chown -R www-data:www-data /opt/web
```

### Create Service File
```bash
sudo nano /etc/systemd/system/ratpanel-web.service
```

Add this content:
```ini
[Unit]
Description=RATPanel Flutter Web Application
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/web
ExecStart=/usr/bin/python3 -m http.server 5000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and Start Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable ratpanel-web
sudo systemctl start ratpanel-web
sudo systemctl status ratpanel-web
```

---

## Nginx Configuration (Optional)

If you want to serve the web panel through Nginx on port 80/443:

### Install Nginx
```bash
sudo apt install nginx -y
```

### Create Nginx Config
```bash
sudo nano /etc/nginx/sites-available/ratpanel-web
```

Add this content:
```nginx
server {
    listen 80;
    server_name YOUR_DOMAIN_OR_IP;

    location / {
        proxy_pass http://localhost:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Enable Site
```bash
sudo ln -s /etc/nginx/sites-available/ratpanel-web /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

---

## Troubleshooting

### Service Won't Start
```bash
# Check logs
sudo journalctl -u ratpanel-web -n 50

# Check if port is in use
sudo lsof -i :5000

# Kill process using port
sudo kill -9 <PID>

# Restart service
sudo systemctl restart ratpanel-web
```

### Permission Denied
```bash
# Fix ownership
sudo chown -R www-data:www-data /opt/web/

# Fix permissions
sudo chmod -R 755 /opt/web/
```

### Port Already in Use
```bash
# Find process using port 5000
sudo lsof -i :5000

# Kill the process
sudo kill -9 <PID>

# Or change port in service file
sudo nano /etc/systemd/system/ratpanel-web.service
# Change: ExecStart=/usr/bin/python3 -m http.server 5000
# To: ExecStart=/usr/bin/python3 -m http.server 5001

sudo systemctl daemon-reload
sudo systemctl restart ratpanel-web
```

### Can't Access from Browser
```bash
# Check if service is running
sudo systemctl status ratpanel-web

# Check firewall
sudo ufw status
sudo ufw allow 5000/tcp

# Test locally
curl http://localhost:5000

# Check if files exist
ls -la /opt/web/
```

---

## Important Notes

- **Build Path**: Flutter builds to `build/web/` directory
- **Server Path**: Files must be in `/opt/web/` on server
- **Port**: Service runs on port 5000 by default
- **Auto-start**: Service starts automatically after server reboot
- **User**: Service runs as `www-data` user
- **Permissions**: Files should be owned by `www-data:www-data`
- **Backup**: Always backup old files before updating

---

## Backup & Restore

### Backup Current Version
```bash
sudo tar -czf /opt/backups/web-backup-$(date +%Y%m%d_%H%M%S).tar.gz /opt/web/
```

### Restore from Backup
```bash
sudo systemctl stop ratpanel-web
sudo rm -rf /opt/web/*
sudo tar -xzf /opt/backups/web-backup-20240219_120000.tar.gz -C /
sudo systemctl start ratpanel-web
```

### Auto Backup Script
```bash
cat > /opt/scripts/backup-web.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/backups/web"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR
tar -czf $BACKUP_DIR/web-backup-$DATE.tar.gz /opt/web/
find $BACKUP_DIR -name "web-backup-*.tar.gz" -mtime +7 -delete
echo "Backup completed: $BACKUP_DIR/web-backup-$DATE.tar.gz"
EOF

chmod +x /opt/scripts/backup-web.sh

# Add to crontab (daily at 3 AM)
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/scripts/backup-web.sh >> /var/log/web-backup.log 2>&1") | crontab -
```

---

## Links

- Panel Repo: https://github.com/zaramoklo-spec/panel
- Server Repo: https://github.com/zaramoklo-spec/server

---

Last Updated: 2024-02-19
