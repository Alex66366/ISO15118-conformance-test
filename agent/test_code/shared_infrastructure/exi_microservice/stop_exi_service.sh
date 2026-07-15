#!/bin/bash
# stop_exi_service.sh
# Stops the EXI Codec Microservice background daemon.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_PID="$SCRIPT_DIR/.exi_service.pid"
CONDA_ENV="${CONDA_ENV:-iso15118}"

echo "[stop_exi_service] Stopping EXI microservice..."

# Method 1: PID file
if [ -f "$SERVICE_PID" ]; then
    PID=$(cat "$SERVICE_PID")
    if kill -0 "$PID" 2>/dev/null; then
        echo "[stop_exi_service] Stopping PID $PID..."
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            echo "[stop_exi_service] Force killing..."
            kill -9 "$PID"
        fi
        echo "[stop_exi_service] Stopped PID $PID."
    else
        echo "[stop_exi_service] PID $PID not running (stale PID file)."
    fi
    rm -f "$SERVICE_PID"
else
    echo "[stop_exi_service] No PID file found."
fi

# Method 2: Find by process name (conda or python)
FOUND=0
for pid in $(pgrep -f "exi_microservice.py" 2>/dev/null || true); do
    if kill -0 "$pid" 2>/dev/null; then
        echo "[stop_exi_service] Found process $pid (pkill)"
        kill "$pid" 2>/dev/null || true
        FOUND=1
    fi
done

# Method 3: Try conda-run spawned processes
if command -v conda >/dev/null 2>&1; then
    for pid in $(pgrep -f "conda.run.*exi_microservice" 2>/dev/null || true); do
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop_exi_service] Found conda process $pid"
            kill "$pid" 2>/dev/null || true
            FOUND=1
        fi
    done
fi

if [ "$FOUND" = "0" ]; then
    echo "[stop_exi_service] No running process found."
fi

echo "[stop_exi_service] Done."
