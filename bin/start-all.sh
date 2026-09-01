#!/usr/bin/env bash
# Launches everything for a normal play session: the tailer, the native
# Vulkan overlay's companion window, and the web UI. Delegates to the
# existing per-piece scripts so each one's pgrep guard and log path stay
# defined in exactly one place.
#
# Requires the overlay layer already installed (overlay-native/install.sh)
# and EQLOG_OVERLAY_ENABLE=1 in the game's Steam launch options for the
# in-game boxes themselves to render -- see start-overlay-native.sh for
# details. Safe to run repeatedly -- won't start duplicates of anything.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$PROJECT_DIR/bin/start-overlay-native.sh"
"$PROJECT_DIR/bin/start-web.sh"
