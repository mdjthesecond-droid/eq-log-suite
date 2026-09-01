#!/usr/bin/env bash
# Launches the live tailer (if not already running) plus the native Vulkan
# overlay's companion app -- the movable/countdown-bar alternative to
# start-mangohud-alerts.sh's text-only rows (see overlay-native/README or
# project memory for the full design).
#
# Requires the layer to be installed first (overlay-native/install.sh) and
# EQLOG_OVERLAY_ENABLE=1 added to the game's Steam launch options for the
# in-game boxes themselves to render -- this script only starts the tailer
# and the position-control companion window, not the layer (which loads
# automatically into the game process via the Vulkan loader once installed
# and enabled).
#
# Safe to run repeatedly -- won't start a second tailer or companion.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
mkdir -p logs

if ! pgrep -f "eq_log_suite\.tailer" > /dev/null; then
    nohup "$PROJECT_DIR/.venv/bin/python" -u -m eq_log_suite.tailer > logs/tailer.log 2>&1 &
    sleep 1
fi

if ! pgrep -f "eq_overlay_companion" > /dev/null; then
    nohup distrobox enter eq-overlay-dev -- "$PROJECT_DIR/overlay-native/build/companion/eq_overlay_companion" \
        > logs/overlay_native_companion.log 2>&1 &
fi
