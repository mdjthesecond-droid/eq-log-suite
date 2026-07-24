#!/usr/bin/env bash
# Launches the live tailer (if not already running) plus the overlay window.
# Safe to run repeatedly -- won't start a second tailer or overlay.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "eq_log_suite\.tailer" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -u -m eq_log_suite.tailer > logs/tailer.log 2>&1 &
    sleep 1
fi

if ! pgrep -f "eq_log_suite\.overlay\.window" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -m eq_log_suite.overlay.window > logs/overlay.log 2>&1 &
fi
