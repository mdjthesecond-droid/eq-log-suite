#include "overlay_state.h"
#include "overlay_json.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <sstream>
#include <thread>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

OverlayState g_state;

std::string PositionFilePath() {
    const char* home = std::getenv("HOME");
    if (!home || !*home) return "";
    return std::string(home) + "/" + kPositionFileRelPath;
}

// Format: one "key x y" line per box, e.g. "rule_12 300.0 150.0". Not JSON
// - the layer already needs a hand-rolled flat-object JSON parser for the
// broadcast protocol (overlay_json.h), but this file's shape is a map of
// two floats, one level deeper than that parser handles (it deliberately
// doesn't do nested objects), and reusing the same "KEY x y" tokenization
// as the live POS wire protocol below means one parsing routine covers
// both instead of two.
void SavePositionsToDiskLocked(OverlayState& state) {
    std::string path = PositionFilePath();
    if (path.empty()) return;

    size_t slash = path.find_last_of('/');
    if (slash != std::string::npos) {
        std::string dir = path.substr(0, slash);
        // mkdir -p equivalent for the (short, known) ~/.config/eq-log-suite
        // path - no <filesystem> dependency needed for two path segments.
        std::string accum;
        for (size_t i = 0; i < dir.size(); i++) {
            accum += dir[i];
            if (dir[i] == '/' || i == dir.size() - 1) {
                mkdir(accum.c_str(), 0755); // ignore EEXIST and friends
            }
        }
    }

    std::ofstream out(path, std::ios::trunc);
    if (!out) {
        fprintf(stderr, "[eq_overlay] failed to write position file %s\n", path.c_str());
        return;
    }
    for (const auto& [key, pos] : state.savedPositions) {
        out << key << " " << pos.first << " " << pos.second << "\n";
    }
}

} // namespace

OverlayState& GetOverlayState() { return g_state; }

std::pair<float, float> ResolveBoxPosition(OverlayState& state, const std::string& key) {
    auto it = state.savedPositions.find(key);
    if (it != state.savedPositions.end()) return it->second;

    // Auto-stack: top-left, offset by ~90px per never-positioned key seen
    // before this one, so a fresh alert_rules key is visible immediately
    // rather than invisible until the user opens the companion app. Each
    // key gets its slot assigned once, on first sight -- a key re-firing
    // after expiring must reuse that same slot, not claim a new one.
    auto seenIt = std::find(state.firstSeenOrder.begin(), state.firstSeenOrder.end(), key);
    size_t index;
    if (seenIt != state.firstSeenOrder.end()) {
        index = static_cast<size_t>(std::distance(state.firstSeenOrder.begin(), seenIt));
    } else {
        index = state.firstSeenOrder.size();
        state.firstSeenOrder.push_back(key);
    }
    return {40.0f + 20.0f * static_cast<float>(index % 5),
            40.0f + 90.0f * static_cast<float>(index)};
}

void LoadSavedPositions() {
    std::string path = PositionFilePath();
    if (path.empty()) return;

    std::ifstream in(path);
    if (!in) return; // fine - no positions configured yet

    std::lock_guard<std::mutex> lock(g_state.mutex);
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream iss(line);
        std::string key;
        float x, y;
        if (iss >> key >> x >> y) {
            g_state.savedPositions[key] = {x, y};
        }
    }
    fprintf(stderr, "[eq_overlay] loaded %zu saved position(s) from %s\n",
            g_state.savedPositions.size(), path.c_str());
}

// ---- Position IPC (companion app -> layer) --------------------------------

namespace {

void PositionHandleClient(int client_fd) {
    char buf[256];
    std::string pending;
    for (;;) {
        ssize_t n = read(client_fd, buf, sizeof(buf));
        if (n <= 0) break;
        pending.append(buf, static_cast<size_t>(n));

        size_t newline;
        while ((newline = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, newline);
            pending.erase(0, newline + 1);

            char keyBuf[128];
            float x, y;
            // "POS <key> <x> <y>" - key is whitespace-free (matches
            // alert_rules keys: "rule_<id>", "combat_state", etc.)
            if (sscanf(line.c_str(), "POS %127s %f %f", keyBuf, &x, &y) == 3) {
                std::string key(keyBuf);
                std::lock_guard<std::mutex> lock(g_state.mutex);
                g_state.savedPositions[key] = {x, y};
                auto it = g_state.boxes.find(key);
                if (it != g_state.boxes.end()) {
                    it->second.x = x;
                    it->second.y = y;
                }
                SavePositionsToDiskLocked(g_state);
                fprintf(stderr, "[eq_overlay] IPC: positioned '%s' at (%.1f, %.1f)\n", key.c_str(), x, y);
            }
        }
    }
    close(client_fd);
}

void PositionServerLoop() {
    int server_fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("[eq_overlay] position socket");
        return;
    }

    unlink(kPositionSocketPath);

    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, kPositionSocketPath, sizeof(addr.sun_path) - 1);

    if (bind(server_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        perror("[eq_overlay] position bind");
        close(server_fd);
        return;
    }
    if (listen(server_fd, 4) != 0) {
        perror("[eq_overlay] position listen");
        close(server_fd);
        return;
    }

    fprintf(stderr, "[eq_overlay] position IPC listening on %s\n", kPositionSocketPath);

    for (;;) {
        int client_fd = accept(server_fd, nullptr, nullptr);
        if (client_fd < 0) continue;
        std::thread(PositionHandleClient, client_fd).detach();
    }
}

} // namespace

void StartPositionIpcThread() {
    static std::once_flag once;
    std::call_once(once, [] { std::thread(PositionServerLoop).detach(); });
}

// ---- Broadcast client (tailer.py -> layer) ---------------------------------

namespace {

std::string GetStr(const JsonObject& obj, const char* key, const std::string& def = "") {
    auto it = obj.find(key);
    if (it == obj.end() || it->second.type != JsonValue::Type::String) return def;
    return it->second.str;
}

bool GetBool(const JsonObject& obj, const char* key, bool def = false) {
    auto it = obj.find(key);
    if (it == obj.end() || it->second.type != JsonValue::Type::Bool) return def;
    return it->second.boolean;
}

// Returns false if the key is absent - callers use this to distinguish
// "duration omitted" (shape 3: sticky, cleared only by "clear":true) from
// any particular value, matching alerts.py's optional-field convention.
bool GetNumber(const JsonObject& obj, const char* key, double& out) {
    auto it = obj.find(key);
    if (it == obj.end() || it->second.type != JsonValue::Type::Number) return false;
    out = it->second.num;
    return true;
}

void HandleAlertMessage(const JsonObject& msg) {
    std::string key = GetStr(msg, "key");
    if (key.empty()) return;

    if (GetBool(msg, "clear")) {
        std::lock_guard<std::mutex> lock(g_state.mutex);
        g_state.boxes.erase(key);
        fprintf(stderr, "[eq_overlay] cleared '%s'\n", key.c_str());
        return;
    }

    AlertBox box;
    box.key = key;
    box.text = GetStr(msg, "text");
    box.countdown = GetBool(msg, "countdown");
    box.receivedAt = std::chrono::steady_clock::now();

    double duration;
    box.hasDuration = GetNumber(msg, "duration", duration);
    box.durationSeconds = box.hasDuration ? static_cast<float>(duration) : 0.0f;

    std::string direction = GetStr(msg, "direction");
    box.hasDirection = !direction.empty();
    box.direction = direction;

    auto colorIt = msg.find("color");
    if (colorIt != msg.end() && colorIt->second.type == JsonValue::Type::NumberArray &&
        colorIt->second.numArray.size() >= 3) {
        box.color[0] = static_cast<float>(colorIt->second.numArray[0]);
        box.color[1] = static_cast<float>(colorIt->second.numArray[1]);
        box.color[2] = static_cast<float>(colorIt->second.numArray[2]);
    }

    std::lock_guard<std::mutex> lock(g_state.mutex);
    auto [x, y] = ResolveBoxPosition(g_state, key);
    box.x = x;
    box.y = y;
    g_state.boxes[key] = std::move(box);
}

// Rendered as a plain sticky AlertBox (shape 3: no hasDuration, so it never
// auto-expires from DrawOverlayFrame's pruning loop) rather than a separate
// box type -- multi-line text via embedded '\n' is all ImGui::TextUnformatted
// needs, so the existing draw path already handles this with no changes.
// Frozen-not-cleared when combat pauses, same convention
// mangohud_writer.py's DPS row already established.
void HandleDpsMessage(const JsonObject& msg) {
    std::string key = GetStr(msg, "key", "dps_meter");

    char line2[128];
    double dpsVal = 0.0;
    GetNumber(msg, "dps", dpsVal);
    double hitPct, critPct;
    bool hasHit = GetNumber(msg, "hit_pct", hitPct);
    bool hasCrit = GetNumber(msg, "crit_pct", critPct);
    if (hasHit && hasCrit) {
        std::snprintf(line2, sizeof(line2), "DPS: %.1f  Hit: %.1f%%  Crit: %.1f%%", dpsVal, hitPct, critPct);
    } else {
        std::snprintf(line2, sizeof(line2), "DPS: %.1f", dpsVal);
    }

    // Total damage taken by whichever mob you're currently hitting, from
    // any source -- not a fight-wide party total, since that would blur
    // together multiple simultaneous targets in an AoE pull. Absent until
    // your first hit lands on something (see MobDamageTracker.current()).
    std::string targetName = GetStr(msg, "target_name");
    std::string line3;
    if (!targetName.empty()) {
        double targetDamage = 0.0;
        GetNumber(msg, "target_damage", targetDamage);
        char buf[128];
        std::snprintf(buf, sizeof(buf), "%s: %.0f dmg taken", targetName.c_str(), targetDamage);
        line3 = buf;
    }

    AlertBox box;
    box.key = key;
    box.text = GetStr(msg, "label") + "\n" + line2 + (line3.empty() ? "" : "\n" + line3);
    box.color[0] = box.color[1] = box.color[2] = 1.0f;
    box.receivedAt = std::chrono::steady_clock::now();
    box.hasDuration = false;

    std::lock_guard<std::mutex> lock(g_state.mutex);
    auto [x, y] = ResolveBoxPosition(g_state, key);
    box.x = x;
    box.y = y;
    g_state.boxes[key] = std::move(box);
}

void BroadcastClientLoop() {
    const char* envPath = std::getenv("EQ_OVERLAY_BROADCAST_SOCKET");
    std::string socketPath = (envPath && *envPath) ? envPath : kDefaultBroadcastSocketPath;

    for (;;) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) {
            perror("[eq_overlay] broadcast socket");
            sleep(2);
            continue;
        }

        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::strncpy(addr.sun_path, socketPath.c_str(), sizeof(addr.sun_path) - 1);

        if (connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
            close(fd);
            sleep(2); // tailer.py not up yet / restarting - matches mangohud_writer.py's reconnect loop
            continue;
        }

        fprintf(stderr, "[eq_overlay] connected to broadcast socket %s\n", socketPath.c_str());

        std::string pending;
        char buf[4096];
        for (;;) {
            ssize_t n = read(fd, buf, sizeof(buf));
            if (n <= 0) break;
            pending.append(buf, static_cast<size_t>(n));

            size_t newline;
            while ((newline = pending.find('\n')) != std::string::npos) {
                std::string line = pending.substr(0, newline);
                pending.erase(0, newline + 1);
                if (line.empty()) continue;

                JsonObject msg;
                if (!ParseFlatJsonObject(line, msg)) continue; // drop malformed lines, same as mangohud_writer.py

                std::string kind = GetStr(msg, "kind");
                if (kind == "alert") HandleAlertMessage(msg);
                else if (kind == "dps") HandleDpsMessage(msg);
                // "zone" kind: out of scope for this phase, ignored.
            }
        }
        close(fd);
        fprintf(stderr, "[eq_overlay] broadcast socket disconnected, reconnecting...\n");
    }
}

} // namespace

void StartBroadcastClientThread() {
    static std::once_flag once;
    std::call_once(once, [] { std::thread(BroadcastClientLoop).detach(); });
}
