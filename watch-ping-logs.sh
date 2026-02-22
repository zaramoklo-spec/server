#!/bin/bash
# Watch only PING related logs in real-time

echo "🔍 Watching PING logs... (Press Ctrl+C to stop)"
echo "================================================"
echo ""

# Follow logs and filter only PING related lines
docker compose logs -f --tail=0 | grep -E "\[PING|\[COMMAND|\[PING-RESPONSE|\[WS\]"
