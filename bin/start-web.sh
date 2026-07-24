#!/usr/bin/env bash
# Launches the web UI (if not already running) and opens it in your browser.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "uvicorn eq_log_suite.web.app:app" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/uvicorn" eq_log_suite.web.app:app --port 8000 > logs/web.log 2>&1 &
    sleep 1
fi

xdg-open http://localhost:8000/ 2>/dev/null || true
