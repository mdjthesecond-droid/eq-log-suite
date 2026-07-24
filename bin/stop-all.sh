#!/usr/bin/env bash
# Stops the tailer, overlay, MangoHud alert writer, and web UI if any are running.
pkill -f "eq_log_suite\.tailer" 2>/dev/null && echo "Stopped tailer." || echo "Tailer wasn't running."
pkill -f "eq_log_suite\.overlay\.window" 2>/dev/null && echo "Stopped overlay." || echo "Overlay wasn't running."
pkill -f "eq_log_suite\.overlay\.mangohud_writer" 2>/dev/null && echo "Stopped MangoHud alert writer." || echo "MangoHud alert writer wasn't running."
pkill -f "uvicorn eq_log_suite.web.app:app" 2>/dev/null && echo "Stopped web UI." || echo "Web UI wasn't running."
