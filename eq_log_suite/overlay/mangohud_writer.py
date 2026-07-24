#!/usr/bin/env python3
"""Writes live DPS/alert text from the tailer's overlay socket to two plain
text files, for two separate MangoHud `exec=cat <file>` lines to display
(one under `custom_text`, one under `custom_text_center`). Unlike
overlay/window.py's GTK window, MangoHud renders inside the game's own
Vulkan/GL swapchain, so it shows up over exclusive fullscreen where the GTK
overlay can't (see README for the full explanation and the MangoHud config
needed alongside this).

DPS output ("kind": "dps") freezes rather than disappears when combat
pauses, same as the GTK overlay's meter. Alert output ("kind": "alert")
clears itself once that alert's duration passes. Multiple concurrent
alerts (different rules/sources, keyed by "key" in the broadcast) are
joined into one line with " | " -- a re-fire of the *same* key (e.g. a
refreshed buff) replaces its own entry in place rather than duplicating.

Run (after `python -m eq_log_suite.tailer` is already running):
    python -m eq_log_suite.overlay.mangohud_writer
"""

import argparse
import json
import queue
import socket
import threading
import time
from pathlib import Path

DEFAULT_SOCKET_PATH = "/tmp/eq_log_suite_overlay.sock"
DEFAULT_DPS_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "mangohud_dps.txt"
DEFAULT_ALERT_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "mangohud_alert.txt"
DEFAULT_LIFETIME = 6.0
TICK_SECONDS = 0.5


def socket_reader_thread(socket_path: str, out_queue: "queue.Queue", stop_event: threading.Event):
    """Runs on a background thread since the main loop needs to keep ticking
    (to expire the current alert) even between messages. Reconnects if the
    tailer isn't up yet or restarts -- same approach as overlay/window.py."""
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


def _format_dps(snap: dict) -> str:
    hit_pct = snap.get("hit_pct")
    crit_pct = snap.get("crit_pct")
    hit_txt = f"{hit_pct:.0f}%" if hit_pct is not None else "--"
    crit_txt = f"{crit_pct:.0f}%" if crit_pct is not None else "--"
    status = "" if snap.get("state") == "active" else " (paused)"
    return f"DPS {snap.get('dps', 0):,.0f}  Hit {hit_txt}  Crit {crit_txt}{status}"


def _write_if_changed(path: Path, text: str, last_written: str | None) -> str:
    if text != last_written:
        path.write_text(f"{text}\n" if text else "")
    return text


def main():
    ap = argparse.ArgumentParser(description="Write live DPS/alert text to files for MangoHud's exec= to display.")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    ap.add_argument("--dps-output", default=str(DEFAULT_DPS_PATH))
    ap.add_argument("--alert-output", default=str(DEFAULT_ALERT_PATH))
    args = ap.parse_args()

    dps_path = Path(args.dps_output)
    alert_path = Path(args.alert_output)
    dps_path.parent.mkdir(parents=True, exist_ok=True)
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    dps_path.write_text("")
    alert_path.write_text("")

    q: "queue.Queue" = queue.Queue()
    stop_event = threading.Event()
    threading.Thread(target=socket_reader_thread, args=(args.socket, q, stop_event), daemon=True).start()

    print(f"[mangohud_writer] watching {args.socket} -> {dps_path} (dps), {alert_path} (alert)")

    alerts: dict[str, dict] = {}  # key -> {"text", "expire_at", "countdown"}
    dps_text = ""
    dps_written = None
    alert_written = None
    while True:
        now = time.monotonic()

        while True:
            try:
                msg = q.get_nowait()
            except queue.Empty:
                break
            kind = msg.get("kind")
            if kind == "alert":
                key = msg.get("key", "default")
                alerts[key] = {
                    "text": msg.get("text", ""),
                    "expire_at": now + float(msg.get("duration", DEFAULT_LIFETIME)),
                    "countdown": bool(msg.get("countdown")),
                }
            elif kind == "dps":
                dps_text = _format_dps(msg)

        alerts = {k: a for k, a in alerts.items() if a["expire_at"] > now}

        # Countdown alerts recompute their displayed text every tick (the
        # remaining-seconds suffix changes even with no new message), rather
        # than showing static text for the alert's whole lifetime.
        parts = []
        for a in alerts.values():
            if a["countdown"]:
                remaining = max(0, round(a["expire_at"] - now))
                parts.append(f"{a['text']} - {remaining}s")
            else:
                parts.append(a["text"])
        display_alert = " | ".join(parts)

        dps_written = _write_if_changed(dps_path, dps_text, dps_written)
        alert_written = _write_if_changed(alert_path, display_alert, alert_written)

        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
