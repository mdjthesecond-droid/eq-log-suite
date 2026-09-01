#!/usr/bin/env bash
# Rebuilds the overlay layer inside the eq-overlay-dev distrobox container
# and installs it as a user-local implicit Vulkan layer, the same way
# MangoHud's own layer lives at /usr/share/vulkan/implicit_layer.d/ (but
# user-scoped, no root needed). The layer only activates when
# EQLOG_OVERLAY_ENABLE=1 is set in the process environment (see
# enable_environment in VkLayer_EQLOG_overlay.json) -- same opt-in pattern
# MangoHud uses via MANGOHUD=1, so it stays silent for every other Vulkan
# app on the system by default.
#
# Safe to re-run after every rebuild during development -- idempotent copy.
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

distrobox enter eq-overlay-dev -- ninja -C build

DEST="$HOME/.local/share/vulkan/implicit_layer.d"
mkdir -p "$DEST"
cp build/layer/libeq_overlay_layer.so "$DEST/"
cp build/layer/VkLayer_EQLOG_overlay.json "$DEST/"

echo "Installed to $DEST"
echo "Activate per-game with EQLOG_OVERLAY_ENABLE=1 in the environment."
