#pragma once

// Shared, mutex-guarded alert-box state. Updated from two independent
// background threads - StartBroadcastClientThread (content, from
// tailer.py's OverlayBroadcaster) and StartPositionIpcThread (placement,
// from the companion app) - and read once per frame by the Vulkan present
// hook (DrawOverlayFrame in overlay_layer.cpp).
//
// Which of the four alert shapes a box renders as is derived at draw time
// from which optional fields are populated, not stored as an explicit
// enum - this mirrors alerts.py's message shape exactly (see
// eq_log_suite/alerts.py's AlertEngine._fire and the [[project_overlay_v2_
// alert_boxes]] memory entry for the full four-shape rationale):
//   countdown && hasDuration  -> shape 1: countdown-with-progress-bar
//   hasDirection              -> shape 2: instant directional cue
//   !hasDuration (sticky)     -> shape 3: state toggle, cleared explicitly
//   otherwise                 -> shape 4: plain timed alert (also covers
//                                the multi-instance case, since it's just
//                                N of these differing only by key)

#include <chrono>
#include <mutex>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

struct AlertBox {
    std::string key;
    std::string text;
    float color[3] = {0.9f, 0.3f, 0.2f};

    bool countdown = false;
    bool hasDuration = false;
    float durationSeconds = 0.0f;
    std::chrono::steady_clock::time_point receivedAt;

    bool hasDirection = false;
    std::string direction;

    float x = 40.0f;
    float y = 40.0f;
};

struct OverlayState {
    std::mutex mutex;
    std::unordered_map<std::string, AlertBox> boxes;
    // Order keys were first seen in, for auto-stacking boxes that have
    // never been given an explicit position (see AutoStackPosition below).
    std::vector<std::string> firstSeenOrder;
    // Positions loaded from disk at startup and updated live by the
    // companion app - kept separate from `boxes` so a position survives
    // even while its box is temporarily inactive (expired/cleared).
    std::unordered_map<std::string, std::pair<float, float>> savedPositions;
};

OverlayState& GetOverlayState();

// Returns the position `key` should use: `savedPositions[key]` if present,
// otherwise a top-left auto-stacked default based on how many never-
// positioned keys have been seen before it (assigned once, on first sight,
// via firstSeenOrder) - so nothing renders invisibly before the user has
// configured it via the companion app.
std::pair<float, float> ResolveBoxPosition(OverlayState& state, const std::string& key);

// Background thread: connects (reconnect-on-failure, mirroring
// eq_log_suite/overlay/mangohud_writer.py's socket_reader_thread) to the
// real alert-broadcast socket and updates GetOverlayState().boxes from
// "kind":"alert" messages. Socket path defaults to tailer.py's
// OverlayBroadcaster path but is overridable via the
// EQ_OVERLAY_BROADCAST_SOCKET env var, so test runs never touch the real
// production socket if tailer.py happens to be running.
void StartBroadcastClientThread();

// Background thread: local server socket accepting newline-terminated
// "POS <key> <x> <y>" messages from the companion app - applied live to
// GetOverlayState() and persisted to kPositionFilePath.
void StartPositionIpcThread();

// Loads kPositionFilePath into GetOverlayState().savedPositions. Called
// once at layer init, before either background thread starts.
void LoadSavedPositions();

inline constexpr const char* kPositionSocketPath = "/tmp/eq_overlay_position.sock";
inline constexpr const char* kDefaultBroadcastSocketPath = "/tmp/eq_log_suite_overlay.sock";
// Deliberately not JSON despite living in a "positions" config - see the
// format note above SavePositionsToDisk in overlay_ipc.cpp for why a
// second parser format wasn't worth it here.
inline constexpr const char* kPositionFileRelPath = ".config/eq-log-suite/overlay_positions.conf";
