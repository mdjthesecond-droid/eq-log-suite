#!/usr/bin/env bash
# Launches the live tailer (if not already running) plus the MangoHud alert
# writer -- the fullscreen-compatible alternative to start-overlay.sh's GTK
# window (see README: "Alerts over fullscreen (MangoHud)").
#
# Requires EQ2's Steam launch options to wrap the game with mangohud and
# point it at a config with an exec=/custom_text= pair reading
# logs/mangohud_alert.txt -- see README for the exact config.
#
# Safe to run repeatedly -- won't start a second tailer or writer.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "eq_log_suite\.tailer" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -u -m eq_log_suite.tailer > logs/tailer.log 2>&1 &
    sleep 1
fi

if ! pgrep -f "eq_log_suite\.overlay\.mangohud_writer" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -m eq_log_suite.overlay.mangohud_writer > logs/mangohud_alerts.log 2>&1 &
fi
