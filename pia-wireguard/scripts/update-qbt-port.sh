#!/bin/bash
PORT="${1:-$(cat /pia-shared/port.dat)}"
(
    for i in {1..90}; do
        if curl -s "http://localhost:8081/api/v2/app/version" > /dev/null 2>&1; then
            curl -s "http://localhost:8081/api/v2/app/setPreferences" -d "json={\"listen_port\":$PORT}"
            echo "[port-sync] Port set to $PORT"
            exit 0
        fi
        sleep 2
    done
    echo "[port-sync] Timeout"
) &
echo "[port-sync] Started background sync for port $PORT"
