#!/usr/bin/env python3
"""Live EQ overlay: transparent, click-through, always-on-top GTK window.
Shows a single-line combat meter (DPS, hit%, crit%) that freezes rather than
resets when combat ends, plus any alert reactions pushed by the tailer.

Evolved from ../../overlay_hello_world.py -- same transparency/click-through/
always-on-top/no-focus-stealing setup (X11 + KWin compositor).

Run (after `python -m eq_log_suite.tailer` is already running):
    python -m eq_log_suite.overlay.window
"""

import argparse
import json
import os
import queue
import socket
import threading
import time
from pathlib import Path

# GTK3 defaults to a native Wayland surface on this (Wayland) session, but
# native Wayland deliberately gives clients no way to force themselves above
# other windows (no equivalent of X11's _NET_WM_STATE_ABOVE) -- it's a
# protocol-level restriction, not something GTK/KWin config can work around.
# Forcing XWayland gets classic X11 always-on-top/window-type behavior back,
# which KWin honors. Must be set before Gdk is imported/initialized.
os.environ.setdefault("GDK_BACKEND", "x11")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402
import cairo  # noqa: E402

DEFAULT_SOCKET_PATH = "/tmp/eq_log_suite_overlay.sock"
DEFAULT_LIFETIME = 6.0
DEFAULT_COLOR = (0.6, 0.6, 0.6)
MAX_ANNOTATIONS = 8

DPS_ACTIVE_COLOR = (0.35, 0.85, 0.4)
DPS_PAUSED_COLOR = (0.55, 0.55, 0.55)
DPS_STALE_SECONDS = 300  # drop a paused meter from the display after this long unused
DPS_PANEL_WIDTH = 420
DPS_METER_HEIGHT = 34
DPS_METER_GAP = 8

LOCK_BTN_X, LOCK_BTN_Y = 10, 50  # clear of the top desktop panel in this environment
LOCK_BTN_W, LOCK_BTN_H = 90, 26

LAYOUT_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "overlay_layout.json"


def load_layout() -> dict:
    try:
        with open(LAYOUT_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_layout(data: dict):
    LAYOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAYOUT_PATH, "w") as f:
        json.dump(data, f)


def socket_reader_thread(socket_path: str, out_queue: "queue.Queue", stop_event: threading.Event):
    """Runs on a background thread since GTK's main loop can't block on
    socket reads. Reconnects if the tailer isn't up yet or restarts."""
    while not stop_event.is_set():
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(socket_path)
            sock.settimeout(None)
            buf = b""
            while not stop_event.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        try:
                            out_queue.put(json.loads(line.decode("utf-8")))
                        except json.JSONDecodeError:
                            pass
        except (FileNotFoundError, ConnectionRefusedError, OSError):
            time.sleep(2)  # tailer not up yet / restarting
        finally:
            if sock is not None:
                sock.close()


class Overlay(Gtk.Window):
    def __init__(self, socket_path: str = DEFAULT_SOCKET_PATH):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)  # never steals keyboard focus from the game
        self.set_focus_on_map(False)
        # KWin gives a *focused fullscreen* window its own stacking layer above
        # ordinary "always on top" windows (by design, so random utility windows
        # can't obstruct fullscreen video/games) -- but it still places the
        # notification layer above that, which is why desktop notification
        # popups show over fullscreen content. Requesting that window type is
        # what actually gets this overlay to render on top of a fullscreen game.
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)

        screen = Gdk.Screen.get_default()
        self.set_default_size(screen.get_width(), screen.get_height())
        self.move(0, 0)

        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        else:
            print(
                "WARNING: no compositor / RGBA visual available -- overlay will not be "
                "transparent. Check that KWin compositing is enabled."
            )

        self.connect("draw", self.on_draw)
        self.connect("realize", self.on_realize)
        self.connect("destroy", Gtk.main_quit)

        # alert-triggered flash messages only -- list of (expires_at, text, color)
        self.annotations: list[tuple[float, str, tuple[float, float, float]]] = []
        # label -> (snapshot dict, local_received_at) -- one persistent meter per
        # live-tailed character, pauses (freezes) rather than disappearing when
        # combat ends, and is dropped only after DPS_STALE_SECONDS of disuse.
        self.dps_meters: dict[str, tuple[dict, float]] = {}
        # label -> current zone name, so you can tell where you are at a glance
        # without needing to have actually fought yet this zone.
        self.current_zones: dict[str, str] = {}

        # The overlay is click-through by design so it never blocks the game.
        # A small always-clickable lock button toggles "unlocked" mode, which
        # carves out just the DPS panel's rectangle as interactive so it can
        # be dragged -- everything else (including that panel's own screen
        # area while locked) still passes clicks straight through to the game.
        layout = load_layout()
        self.locked = True
        self.dps_panel_pos = tuple(layout.get("dps_panel_pos", (screen.get_width() - DPS_PANEL_WIDTH - 60, 60)))
        self.dragging = False
        self._drag_offset = (0.0, 0.0)

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event", self.on_button_press)
        self.connect("button-release-event", self.on_button_release)
        self.connect("motion-notify-event", self.on_motion)

        self._queue: "queue.Queue" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=socket_reader_thread, args=(socket_path, self._queue, self._stop_event), daemon=True
        )
        self._thread.start()
        self.connect("destroy", lambda *_: self._stop_event.set())

        GLib.timeout_add(150, self.tick)

    def on_realize(self, widget):
        self._update_input_shape()

    def _dps_panel_height(self) -> float:
        n = max(len(set(self.dps_meters) | set(self.current_zones)), 1)  # reserve at least one row
        return n * DPS_METER_HEIGHT + (n - 1) * DPS_METER_GAP

    def _update_input_shape(self):
        window = self.get_window()
        if window is None:
            return
        # The lock button is always live so you can always get back in to
        # relock it; the DPS panel only accepts clicks while unlocked.
        region = cairo.Region(cairo.RectangleInt(LOCK_BTN_X, LOCK_BTN_Y, LOCK_BTN_W, LOCK_BTN_H))
        if not self.locked:
            px, py = int(self.dps_panel_pos[0]), int(self.dps_panel_pos[1])
            region.union(cairo.Region(cairo.RectangleInt(px, py, DPS_PANEL_WIDTH, int(self._dps_panel_height()))))
        window.input_shape_combine_region(region, 0, 0)

    @staticmethod
    def _hit(x, y, rx, ry, rw, rh) -> bool:
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def on_button_press(self, widget, event):
        if self._hit(event.x, event.y, LOCK_BTN_X, LOCK_BTN_Y, LOCK_BTN_W, LOCK_BTN_H):
            self.locked = not self.locked
            if self.locked:
                save_layout({"dps_panel_pos": list(self.dps_panel_pos)})
            self._update_input_shape()
            self.queue_draw()
            return True
        if not self.locked and self._hit(
            event.x, event.y, *self.dps_panel_pos, DPS_PANEL_WIDTH, self._dps_panel_height()
        ):
            self.dragging = True
            self._drag_offset = (event.x - self.dps_panel_pos[0], event.y - self.dps_panel_pos[1])
            return True
        return False

    def on_motion(self, widget, event):
        if self.dragging:
            self.dps_panel_pos = (event.x - self._drag_offset[0], event.y - self._drag_offset[1])
            self._update_input_shape()
            self.queue_draw()
            return True
        return False

    def on_button_release(self, widget, event):
        if self.dragging:
            self.dragging = False
            save_layout({"dps_panel_pos": list(self.dps_panel_pos)})
            return True
        return False

    def tick(self):
        now = time.monotonic()
        changed = False
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            self._handle_message(msg, now)

        before = len(self.annotations)
        self.annotations = [a for a in self.annotations if a[0] > now]

        before_meters = len(self.dps_meters)
        self.dps_meters = {
            label: (snap, seen_at)
            for label, (snap, seen_at) in self.dps_meters.items()
            if now - seen_at < DPS_STALE_SECONDS
        }

        if changed or len(self.annotations) != before or len(self.dps_meters) != before_meters:
            self.queue_draw()
        return True  # keep the timeout running

    def _handle_message(self, msg: dict, now: float):
        kind = msg.get("kind")
        if kind == "dps":
            self.dps_meters[msg["label"]] = (msg, now)
            return
        if kind == "zone":
            self.current_zones[msg["label"]] = msg["zone"]
            return
        if kind == "alert":
            text = msg.get("text", "")
            color = tuple(msg.get("color", DEFAULT_COLOR))
            duration = float(msg.get("duration", DEFAULT_LIFETIME))
            self.annotations.append((now + duration, text, color))
            self.annotations = self.annotations[-MAX_ANNOTATIONS:]

    def on_draw(self, widget, ctx: cairo.Context):
        ctx.set_source_rgba(0, 0, 0, 0)
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

        self._draw_alerts(ctx)
        self._draw_dps_meters(ctx)
        self._draw_lock_button(ctx)
        return False

    def _draw_alerts(self, ctx: cairo.Context):
        if not self.annotations:
            return
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(16)

        x, y = 60, 60
        line_height = 26
        for _, text, color in self.annotations:
            r, g, b = color
            extents = ctx.text_extents(text)
            pad = 8
            box_w = extents.width + pad * 2
            box_h = extents.height + pad * 2

            ctx.set_source_rgba(0, 0, 0, 0.55)
            self.rounded_rect(ctx, x, y, box_w, box_h, 6)
            ctx.fill()

            ctx.set_source_rgba(r, g, b, 0.95)
            ctx.rectangle(x, y, 4, box_h)
            ctx.fill()

            ctx.set_source_rgba(1, 1, 1, 1)
            ctx.move_to(x + pad, y + pad + extents.height)
            ctx.show_text(text)

            y += line_height

    def _draw_lock_button(self, ctx: cairo.Context):
        if self.locked:
            ctx.set_source_rgba(0, 0, 0, 0.55)
        else:
            ctx.set_source_rgba(0.15, 0.55, 0.85, 0.9)
        self.rounded_rect(ctx, LOCK_BTN_X, LOCK_BTN_Y, LOCK_BTN_W, LOCK_BTN_H, 6)
        ctx.fill()

        ctx.set_source_rgba(1, 1, 1, 1)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(12)
        label = "LOCKED" if self.locked else "UNLOCKED"
        extents = ctx.text_extents(label)
        ctx.move_to(
            LOCK_BTN_X + (LOCK_BTN_W - extents.width) / 2,
            LOCK_BTN_Y + (LOCK_BTN_H + extents.height) / 2,
        )
        ctx.show_text(label)

    def _draw_dps_meters(self, ctx: cairo.Context):
        labels = set(self.dps_meters) | set(self.current_zones)
        if not labels and self.locked:
            return  # nothing to show yet, and no need to preview an empty panel while locked

        x, y = self.dps_panel_pos
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        if not labels:
            # unlocked with no combat/zone seen yet -- show a placeholder so it
            # can still be positioned in advance
            self._draw_panel_box(ctx, x, y, DPS_PAUSED_COLOR, "DPS meter -- drag to reposition")
            return

        for label in sorted(labels):
            zone = self.current_zones.get(label)
            zone_suffix = f"  [{zone}]" if zone else ""
            entry = self.dps_meters.get(label)

            if entry is None:
                # zone known but no combat seen yet this session
                line = f"{label}{zone_suffix}  DPS --"
                color = DPS_PAUSED_COLOR
            else:
                snap, _seen_at = entry
                active = snap.get("state") == "active"
                color = DPS_ACTIVE_COLOR if active else DPS_PAUSED_COLOR
                hit_pct = snap.get("hit_pct")
                crit_pct = snap.get("crit_pct")
                hit_txt = f"{hit_pct:.0f}%" if hit_pct is not None else "--"
                crit_txt = f"{crit_pct:.0f}%" if crit_pct is not None else "--"
                status = " (paused)" if not active else ""
                line = (
                    f"{label}{zone_suffix}  DPS {snap.get('dps', 0):,.0f}   "
                    f"Hit {hit_txt}   Crit {crit_txt}{status}"
                )
            self._draw_panel_box(ctx, x, y, color, line)
            y += DPS_METER_HEIGHT + DPS_METER_GAP

    def _draw_panel_box(self, ctx, x, y, color, line):
        box_w, box_h = DPS_PANEL_WIDTH, DPS_METER_HEIGHT

        ctx.set_source_rgba(0, 0, 0, 0.6)
        self.rounded_rect(ctx, x, y, box_w, box_h, 8)
        ctx.fill()

        ctx.set_source_rgba(*color, 0.95)
        ctx.rectangle(x, y, box_w, 3)
        ctx.fill()

        if not self.locked:
            # dashed outline signals "this is draggable right now"
            ctx.set_dash([4, 3])
            ctx.set_source_rgba(1, 1, 1, 0.5)
            ctx.set_line_width(1.5)
            self.rounded_rect(ctx, x, y, box_w, box_h, 8)
            ctx.stroke()
            ctx.set_dash([])

        ctx.set_source_rgba(*color, 1)
        ctx.set_font_size(15)
        extents = ctx.text_extents(line)
        ctx.move_to(x + 10, y + (box_h + extents.height) / 2)
        ctx.show_text(line)

    @staticmethod
    def rounded_rect(ctx, x, y, w, h, r):
        ctx.new_sub_path()
        ctx.arc(x + w - r, y + r, r, -90 * 3.14159 / 180, 0)
        ctx.arc(x + w - r, y + h - r, r, 0, 90 * 3.14159 / 180)
        ctx.arc(x + r, y + h - r, r, 90 * 3.14159 / 180, 180 * 3.14159 / 180)
        ctx.arc(x + r, y + r, r, 180 * 3.14159 / 180, 270 * 3.14159 / 180)
        ctx.close_path()


def main():
    ap = argparse.ArgumentParser(description="Live EQ overlay window.")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    args = ap.parse_args()
    win = Overlay(socket_path=args.socket)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
