#!/usr/bin/env bash
# Launches just the live tailer (no overlay window). This is the normal
# day-to-day launcher now that the overlay isn't in regular use (KWin
# prioritizes a focused fullscreen game above it -- see README/session notes).
# Safe to run repeatedly -- won't start a second tailer.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "eq_log_suite\.tailer" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -u -m eq_log_suite.tailer > logs/tailer.log 2>&1 &
fi
