#!/usr/bin/env bash
# Launches the item-screenshot OCR watcher (see eq_log_suite/item_capture_watcher.py).
# Safe to run repeatedly -- won't start a second one.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "eq_log_suite\.item_capture_watcher" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -u -m eq_log_suite.item_capture_watcher > logs/item_capture_watcher.log 2>&1 &
fi
