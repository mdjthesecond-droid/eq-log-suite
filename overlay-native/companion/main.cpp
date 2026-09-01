// Companion app, phase 2: connects read-only to the real alert-broadcast
// socket (same one tailer.py's OverlayBroadcaster and the layer's
// BroadcastClientThread use) to learn which alert keys are currently
// active, lists them with their last-seen text, and gives each an X/Y
// spin-box pair that sends "POS <key> <x> <y>" to the layer's position
// IPC socket on change. This is real, working position control - just
// not the fancier drag-on-a-scaled-canvas UX, which is a later
// refinement once this simpler control loop is proven (see
// [[project_overlay_v2_alert_boxes]] in memory for why that split was
// chosen: avoid input-hooking risk, ship working functionality now).
//
// Phase 3: rows are no longer purely reactive. A fixed set of boxes (see
// kBoxes below, matching eq_log_suite/overlay/boxes.py's BOXES) always
// gets a row with a spin-box pair up front, even before anything has
// broadcast into it - fixing the original gap where a key only appeared
// once its content happened to be actively firing at the moment this
// window was open (DPS only broadcasts while mid-combat, so the row could
// be entirely absent when you reached for it). Ad-hoc rows for un-assigned
// per-rule alerts (key "rule_<id>") still appear reactively exactly as
// before once they first fire.

#include <gtk/gtk.h>

#include "overlay_json.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <map>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace {

constexpr const char* kPositionSocketPath = "/tmp/eq_overlay_position.sock";
constexpr const char* kDefaultBroadcastSocketPath = "/tmp/eq_log_suite_overlay.sock";
// Same config dir/format overlay_state.h's kPositionFileRelPath uses -
// kept as a separate literal here rather than a shared header, matching
// this file's existing practice of duplicating the two socket-path
// constants above instead of including layer/overlay_state.h (companion
// and layer are deliberately separate meson targets - see
// overlay-native/companion/meson.build).
constexpr const char* kPositionFileRelPath = ".config/eq-log-suite/overlay_positions.conf";

struct BoxDef {
    std::string key;
    std::string name;
};

// The fixed set of boxes - assigned to, not created on the fly. Must be
// kept in sync by hand with eq_log_suite/overlay/boxes.py's BOXES list;
// see that file's docstring for why this is duplicated rather than a
// shared config source (companion is a separate C++ binary with no
// shared config/DB access).
const std::vector<BoxDef> kBoxes = {
    {"dps_meter", "DPS"},
    {"alert_box_1", "Alert Box 1"},
    {"alert_box_2", "Alert Box 2"},
    {"alert_box_3", "Alert Box 3"},
    {"alert_box_4", "Alert Box 4"},
};

std::string HomeConfigPath(const char* relPath) {
    const char* home = std::getenv("HOME");
    if (!home || !*home) return "";
    return std::string(home) + "/" + relPath;
}

// Loads currently-saved positions (same "key x y" format the layer writes
// via SavePositionsToDiskLocked in overlay_ipc.cpp) so a box's spin
// buttons start at wherever it's actually rendering, instead of always
// defaulting to the same top-left guess.
std::map<std::string, std::pair<double, double>> LoadSavedPositions() {
    std::map<std::string, std::pair<double, double>> out;
    std::string path = HomeConfigPath(kPositionFileRelPath);
    if (path.empty()) return out;
    std::ifstream in(path);
    if (!in) return out;

    std::string key;
    double x, y;
    while (in >> key >> x >> y) out[key] = {x, y};
    return out;
}

// In-memory mirror of the position file, seeded from LoadSavedPositions()
// at startup and kept current by SavePositionToDisk below - the companion
// is now the durable writer, not just a relay: the layer only persists a
// position while it's actually running and reachable over
// kPositionSocketPath (see SendPosition), so a move made while EQ isn't
// running would otherwise vanish instead of surviving to the next
// session.
std::map<std::string, std::pair<double, double>> g_savedPositions;

// Same file/format the layer's SavePositionsToDiskLocked (overlay_ipc.cpp)
// writes - duplicated here rather than shared for the same reason the
// path/socket constants above are duplicated (separate meson targets, no
// shared config source). Writing it directly from the companion means a
// position survives to next session even if the live layer was never
// reachable to persist it itself.
void SavePositionToDisk(const std::string& key, double x, double y) {
    g_savedPositions[key] = {x, y};

    std::string path = HomeConfigPath(kPositionFileRelPath);
    if (path.empty()) return;

    size_t slash = path.find_last_of('/');
    if (slash != std::string::npos) {
        std::string dir = path.substr(0, slash);
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
        fprintf(stderr, "failed to write position file %s\n", path.c_str());
        return;
    }
    for (const auto& [savedKey, pos] : g_savedPositions) {
        out << savedKey << " " << pos.first << " " << pos.second << "\n";
    }
}

void SendPosition(const std::string& key, double x, double y) {
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("socket");
        return;
    }

    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, kPositionSocketPath, sizeof(addr.sun_path) - 1);

    if (connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        perror("connect (is the layer running with a Vulkan app?)");
        close(fd);
        return;
    }

    char msg[192];
    int len = snprintf(msg, sizeof(msg), "POS %s %.1f %.1f\n", key.c_str(), x, y);
    write(fd, msg, static_cast<size_t>(len));
    close(fd);

    g_print("Sent POS %s %.1f %.1f\n", key.c_str(), x, y);
}

// One row per fixed box: display name + last-seen text, and an X/Y
// spin-box pair. The row set is built at startup from kBoxes - nothing
// here ever decides at runtime whether a row "is needed"; every box in
// the fixed set gets a row, unconditionally, before the broadcast thread
// even starts. The broadcast thread's only job after that is to fill in
// the label text for a row that's already there.
struct KeyRow {
    GtkWidget* box = nullptr;
    GtkWidget* label = nullptr;
    GtkWidget* xSpin = nullptr;
    GtkWidget* ySpin = nullptr;
    std::string displayName;
};

struct AppState {
    GtkWidget* listBox = nullptr;
    // Only ever touched on the GTK main thread: BroadcastReaderThread never
    // reads/writes `rows` directly, it posts a RowUpdate via g_idle_add and
    // ApplyRowUpdate (which always runs on the main loop) does the mutation.
    std::map<std::string, KeyRow> rows;
};

// Builds one row for `key`/`displayName` at the given initial position and
// appends it to app->listBox. Called only during startup, once per box in
// kBoxes - never from the broadcast path.
void CreateRow(AppState* app, const std::string& key, const std::string& displayName, double x, double y) {
    KeyRow row;
    row.displayName = displayName;
    row.box = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    row.label = gtk_label_new(displayName.c_str());
    gtk_label_set_xalign(GTK_LABEL(row.label), 0.0f);
    gtk_widget_set_size_request(row.label, 320, -1);

    row.xSpin = gtk_spin_button_new_with_range(0, 4000, 5);
    row.ySpin = gtk_spin_button_new_with_range(0, 4000, 5);
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(row.xSpin), x);
    gtk_spin_button_set_value(GTK_SPIN_BUTTON(row.ySpin), y);

    std::string* keyCopy = new std::string(key); // leaked deliberately for app lifetime - fine for a small companion tool

    gtk_box_pack_start(GTK_BOX(row.box), row.label, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(row.box), gtk_label_new("X:"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(row.box), row.xSpin, FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(row.box), gtk_label_new("Y:"), FALSE, FALSE, 0);
    gtk_box_pack_start(GTK_BOX(row.box), row.ySpin, FALSE, FALSE, 0);

    // One combined handler per spin button, added after both spins exist,
    // so a change to either sends the current pair - simpler than
    // threading partial state through two separate callbacks.
    auto* pair = new std::pair<GtkWidget*, GtkWidget*>(row.xSpin, row.ySpin);
    auto onChange = +[](GtkSpinButton*, gpointer userData) {
        auto* ctx = static_cast<std::pair<std::pair<GtkWidget*, GtkWidget*>*, std::string*>*>(userData);
        double px = gtk_spin_button_get_value(GTK_SPIN_BUTTON(ctx->first->first));
        double py = gtk_spin_button_get_value(GTK_SPIN_BUTTON(ctx->first->second));
        SavePositionToDisk(*ctx->second, px, py);
        SendPosition(*ctx->second, px, py);
    };
    auto* ctx = new std::pair<std::pair<GtkWidget*, GtkWidget*>*, std::string*>(pair, keyCopy);
    g_signal_connect(row.xSpin, "value-changed", G_CALLBACK(onChange), ctx);
    g_signal_connect(row.ySpin, "value-changed", G_CALLBACK(onChange), ctx);

    gtk_box_pack_start(GTK_BOX(app->listBox), row.box, FALSE, FALSE, 0);
    gtk_widget_show_all(row.box);

    app->rows[key] = row;
}

struct RowUpdate {
    AppState* app;
    std::string key;
    std::string text;
};

// Called on the GTK main thread via g_idle_add from the socket-reader
// thread below - GTK widgets aren't thread-safe to touch directly from a
// background thread. Every box row already exists by the time this runs
// (built at startup from kBoxes); a key with no matching row is an
// un-assigned rule's own implicit box (see alerts.py's "own box (default)"
// fallback) and is intentionally left un-positionable here rather than
// spawning a row for it - assign it to a fixed box via /alerts instead.
gboolean ApplyRowUpdate(gpointer data) {
    auto* update = static_cast<RowUpdate*>(data);
    auto it = update->app->rows.find(update->key);
    if (it != update->app->rows.end()) {
        std::string labelText = it->second.displayName + "  \"" + update->text + "\"";
        gtk_label_set_text(GTK_LABEL(it->second.label), labelText.c_str());
    }
    delete update;
    return G_SOURCE_REMOVE;
}

std::string GetStr(const JsonObject& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || it->second.type != JsonValue::Type::String) return "";
    return it->second.str;
}

void BroadcastReaderThread(AppState* app) {
    const char* envPath = std::getenv("EQ_OVERLAY_BROADCAST_SOCKET");
    std::string socketPath = (envPath && *envPath) ? envPath : kDefaultBroadcastSocketPath;

    for (;;) {
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (fd < 0) {
            sleep(2);
            continue;
        }
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::strncpy(addr.sun_path, socketPath.c_str(), sizeof(addr.sun_path) - 1);

        if (connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
            close(fd);
            sleep(2);
            continue;
        }

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
                if (!ParseFlatJsonObject(line, msg)) continue;
                std::string kind = GetStr(msg, "kind");
                if (kind != "alert" && kind != "dps") continue;
                std::string key = GetStr(msg, "key");
                if (key.empty()) continue;

                auto* update = new RowUpdate{app, key, GetStr(msg, "text")};
                g_idle_add(ApplyRowUpdate, update);
            }
        }
        close(fd);
    }
}

void Activate(GtkApplication* gtkApp, gpointer userData) {
    auto* app = static_cast<AppState*>(userData);

    GtkWidget* window = gtk_application_window_new(gtkApp);
    gtk_window_set_title(GTK_WINDOW(window), "eq-log-suite overlay - companion");
    gtk_window_set_default_size(GTK_WINDOW(window), 480, 320);

    GtkWidget* vbox = gtk_box_new(GTK_ORIENTATION_VERTICAL, 4);

    GtkWidget* scroll = gtk_scrolled_window_new(nullptr, nullptr);
    app->listBox = gtk_box_new(GTK_ORIENTATION_VERTICAL, 4);
    gtk_container_add(GTK_CONTAINER(scroll), app->listBox);
    gtk_box_pack_start(GTK_BOX(vbox), scroll, TRUE, TRUE, 0);
    gtk_container_add(GTK_CONTAINER(window), vbox);

    // Every fixed box gets its row now, up front - not discovered
    // piecemeal as broadcast traffic happens to arrive.
    std::map<std::string, std::pair<double, double>> savedPositions = LoadSavedPositions();
    g_savedPositions = savedPositions;
    for (size_t i = 0; i < kBoxes.size(); i++) {
        const BoxDef& def = kBoxes[i];
        double x = 40.0 + 20.0 * static_cast<double>(i % 5);
        double y = 40.0 + 90.0 * static_cast<double>(i);
        auto posIt = savedPositions.find(def.key);
        if (posIt != savedPositions.end()) {
            x = posIt->second.first;
            y = posIt->second.second;
        }
        CreateRow(app, def.key, def.name, x, y);
        gtk_label_set_text(GTK_LABEL(app->rows[def.key].label), (def.name + "  (no data yet)").c_str());
    }

    gtk_widget_show_all(window);

    std::thread(BroadcastReaderThread, app).detach();
}

} // namespace

int main(int argc, char** argv) {
    AppState app;
    GtkApplication* gtkApp = gtk_application_new("suite.eqlog.overlay.companion", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(gtkApp, "activate", G_CALLBACK(Activate), &app);
    int status = g_application_run(G_APPLICATION(gtkApp), argc, argv);
    g_object_unref(gtkApp);
    return status;
}
