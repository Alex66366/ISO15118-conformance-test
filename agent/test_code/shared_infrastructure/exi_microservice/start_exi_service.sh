#!/bin/bash
# start_exi_service.sh
# Launches the EXI Codec Microservice as a background daemon.
#
# Environment resolution (fast — avoids slow "conda run"):
#   1. conda iso15118 env Python (has py4j)  ← RECOMMENDED
#   2. system Python 3 (requires py4j installed)
#
# Override:
#   PYTHON=/path/to/python  ./start_exi_service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-}"      # Set to skip auto-detection
CONDA_ENV="${CONDA_ENV:-}"
SERVICE_PID="$SCRIPT_DIR/.exi_service.pid"
LOG_FILE="${LOG_FILE:-/tmp/exi_microservice.log}"
PORT="${EXI_PORT:-9000}"

# Auto-detect conda env that has py4j installed (priority order).
# This makes the script work whether or not `conda activate iso15118` was run.
if [ -z "$CONDA_ENV" ] && command -v conda >/dev/null 2>&1; then
    # Try known env names first
    for CANDIDATE in iso15118 iso15118-3 ev-py311 venv-py311; do
        if conda env list 2>/dev/null | awk '{print $1}' | grep -qx "$CANDIDATE"; then
            CONDA_ENV="$CANDIDATE"
            break
        fi
    done
    # Fallback: scan all envs, pick first one that has py4j importable
    if [ -z "$CONDA_ENV" ]; then
        while IFS= read -r PREFIX; do
            [ -x "$PREFIX/bin/python3" ] || continue
            if "$PREFIX/bin/python3" -c "import py4j" 2>/dev/null; then
                CONDA_ENV="$(basename "$PREFIX")"
                break
            fi
        done < <(conda env list 2>/dev/null | awk '/^#/{next} NF>=2 && $2 ~ /^\// {print $2}')
    fi
    [ -n "$CONDA_ENV" ] && export CONDA_ENV
fi

# ── Already running ─────────────────────────────────────────────────────
if [ -f "$SERVICE_PID" ]; then
    PID=$(cat "$SERVICE_PID")
    if kill -0 "$PID" 2>/dev/null; then
        echo "[start_exi_service] Already running with PID $PID"
        exit 0
    fi
    echo "[start_exi_service] Stale PID file, removing..."
    rm -f "$SERVICE_PID"
fi

# ── JAR check (vendor-first, legacy fallback) ─────────────────────────
# Prefer the vendored JAR. Fall back to the legacy top-level copy, which
# may still exist in older checkouts. Either way the bridge code in
# exi_codec_handler.py will pick the same path.
JAR=""
for candidate in \
    "$SCRIPT_DIR/vendor/iso15118/shared/EXICodec.jar" \
    "$SCRIPT_DIR/EXICodec.jar"; do
    if [ -f "$candidate" ] && [ -s "$candidate" ]; then
        JAR="$candidate"
        break
    fi
done
if [ -z "$JAR" ]; then
    echo "[start_exi_service] ERROR: EXICodec.jar missing (looked in vendor/iso15118/shared/ and top-level)"
    exit 1
fi
JAR_SIZE=$(stat -c%s "$JAR" 2>/dev/null || echo "0")
if [ "$JAR_SIZE" -lt 1000000 ]; then
    echo "[start_exi_service] ERROR: EXICodec.jar too small ($JAR_SIZE bytes at $JAR)"
    exit 1
fi

# ── Python resolution ────────────────────────────────────────────────
# Fast path: parse "conda env list" to get prefix, then use Python directly.
# Avoids "conda run" which is slow in some environments.
PYTHON_CMD=""
PYTHON_DESC=""

if [ -n "$PYTHON" ]; then
    # Explicit override
    if [ -x "$PYTHON" ]; then
        PYTHON_CMD="$PYTHON"
        PYTHON_DESC="user-specified ($PYTHON)"
    else
        echo "[start_exi_service] ERROR: PYTHON='$PYTHON' not executable"
        exit 1
    fi
elif command -v conda >/dev/null 2>&1; then
    # Get conda env prefix via env list (fast).
    # Handle both "name  /path" and "name *  /path" (active env) formats.
    CONDA_PREFIX=$(conda env list 2>/dev/null \
        | awk -v env="$CONDA_ENV" '$1 == env || $1 == env"*" {print $NF; exit}' || true)
    if [ -n "$CONDA_PREFIX" ] && [ -x "$CONDA_PREFIX/bin/python3" ]; then
        PYTHON_CMD="$CONDA_PREFIX/bin/python3"
        PYTHON_DESC="conda env '$CONDA_ENV' ($CONDA_PREFIX)"
    fi
fi

if [ -z "$PYTHON_CMD" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python3"
        PYTHON_DESC="system python3"
    else
        echo "[start_exi_service] ERROR: python3 not found"
        exit 1
    fi
fi

# ── py4j check ───────────────────────────────────────────────────
PY4J_VER=$("$PYTHON_CMD" -c "import py4j; print(py4j.__version__)" 2>/dev/null)
if [ -z "$PY4J_VER" ]; then
    echo "[start_exi_service] ERROR: py4j not importable with: $PYTHON_CMD"
    echo "[start_exi_service] Fix: $PYTHON_CMD -m pip install py4j"
    exit 1
fi

# ── Java check ───────────────────────────────────────────────────────
if ! command -v java >/dev/null 2>&1; then
    echo "[start_exi_service] ERROR: Java not found"
    exit 1
fi

# ── Launch ───────────────────────────────────────────────────────────
echo "[start_exi_service] Starting EXI microservice..."
echo "[start_exi_service]   Port  : $PORT"
echo "[start_exi_service]   JAR   : $JAR ($JAR_SIZE bytes)"
echo "[start_exi_service]   Python: $PYTHON_CMD"
echo "[start_exi_service]   py4j  : $PY4J_VER"
echo "[start_exi_service]   Log   : $LOG_FILE"

cd "$SCRIPT_DIR"

# Isolated wrapper: avoids background job inheriting shell state.
# Keep the wrapper around (do NOT rm) so the launched bash can read it.
WRAPPER="/tmp/exi_wrapper_$$.sh"
cat > "$WRAPPER" <<EOF
#!/bin/bash
exec "$PYTHON_CMD" "$SCRIPT_DIR/exi_microservice.py" --port $PORT
EOF
chmod +x "$WRAPPER"

# Launch fully detached so this script's exit doesn't kill the child.
setsid bash "$WRAPPER" >> "$LOG_FILE" 2>&1 < /dev/null &
NEW_PID=$!
disown 2>/dev/null || true
echo "$NEW_PID" > "$SERVICE_PID"

# ── Wait for JVM + py4j gateway to start ──────────────────────────
echo "[start_exi_service] Waiting for microservice (PID=$NEW_PID, timeout=30s)..."
for i in $(seq 1 30); do
    sleep 1
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "[start_exi_service] Process exited at ${i}s. Log:"
        tail -30 "$LOG_FILE" 2>/dev/null || echo "  (log empty)"
        echo ""
        echo "[start_exi_service] === Diagnostic ==="
        echo "Wrapper:  $WRAPPER  (exists: $([ -f "$WRAPPER" ] && echo yes || echo no))"
        echo "Log file: $LOG_FILE ($(wc -l < "$LOG_FILE" 2>/dev/null || echo 0) lines)"
        echo "Last 5 exi/python processes:"
        ps -ef | grep -E "exi_microservice|conda.run.*exi|start_exi_service" | grep -v grep | tail -5
        echo ""
        echo "[start_exi_service] === Try running wrapper manually ==="
        if [ -f "$WRAPPER" ]; then
            echo "  bash $WRAPPER"
        else
            echo "  Wrapper missing — would have been:"
            echo "  exec \"$PYTHON_CMD\" \"$SCRIPT_DIR/exi_microservice.py\" --port $PORT"
        fi
        rm -f "$SERVICE_PID"
        exit 1
    fi
    if nc -z localhost "$PORT" 2>/dev/null; then
        echo "[start_exi_service] SUCCESS: PID=$NEW_PID, port $PORT OPEN"
        exit 0
    fi
    [ $((i % 5)) -eq 0 ] && echo "[start_exi_service]   Still waiting... (${i}s)"
done

if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[start_exi_service] TIMEOUT: Process alive PID=$NEW_PID, port not open yet."
    echo "[start_exi_service]   JVM may still be starting. Check log:"
    echo "  tail -f $LOG_FILE"
else
    echo "[start_exi_service] Process exited. Log:"
    tail -20 "$LOG_FILE"
    rm -f "$SERVICE_PID"
    exit 1
fi
