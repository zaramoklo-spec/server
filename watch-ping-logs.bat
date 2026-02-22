@echo off
REM Watch only PING related logs in real-time (Windows)

echo 🔍 Watching PING logs... (Press Ctrl+C to stop)
echo ================================================
echo.

REM Follow logs and filter only PING related lines
docker compose logs -f --tail=0 | findstr /C:"[PING" /C:"[COMMAND" /C:"[PING-RESPONSE" /C:"[WS]"
