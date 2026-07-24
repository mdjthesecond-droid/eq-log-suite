"""Live log tailer: polls every log_sources row marked live=TRUE, parses new
lines as they're appended, writes them to MySQL via the same path the bulk
importer uses, runs the alert engine on each parsed event, and broadcasts
live DPS/hit%/crit% snapshots + alert reactions to any connected overlay
process over a local Unix socket.

Usage:
    python -m eq_log_suite.tailer

Add a file to live-tail with:
    python -m eq_log_suite.importer <path> --live
(this also does the initial historical catch-up import).
"""

import argparse
import asyncio
import json
import os
import signal
import time
from pathlib import Path

from eq_log_suite import db, discovery, ingest
from eq_log_suite.alerts import AlertEngine
from eq_log_suite.parsers.registry import get_parser

POLL_INTERVAL = 0.25
DEFAULT_SOCKET_PATH = "/tmp/eq_log_suite_overlay.sock"
DISCOVERY_INTERVAL_SECONDS = 300  # re-scan for new/rotated log files every 5 minutes
# The web UI's "rescan now" button sends SIGUSR1 to this PID to wake the
# discovery loop immediately instead of waiting for the next timer tick --
# see eq_log_suite.web.app's /import/rescan route.
PID_PATH = Path(__file__).resolve().parent.parent / "logs" / "tailer.pid"

# DPS meter tuning: how it decides combat has ended (freeze, don't reset to
# zero) vs. is still ongoing between hits, and how often it re-broadcasts a
# still-active encounter so the on-screen timer visibly keeps ticking even
# between hits.
COMBAT_TIMEOUT_SECONDS = 6.0
DPS_TICK_INTERVAL = 1.0


class DPSTracker:
    """Tracks one character's outgoing damage, melee accuracy, and crit rate
    for a live combat meter. Combat is considered over -- and the displayed
    numbers frozen rather than reset -- after COMBAT_TIMEOUT_SECONDS with no
    new activity from this character, matching the usual EQ-parser
    convention (GamParse/EQLogParser use a similar ~6s window).

    Hit%/crit% are computed from melee swings only (event_type == 'melee':
    autoattack plus weapon-skill specials like backstab), not spell/ability
    damage -- that's the conventional meaning of "hit rate" in EQ parlance.
    """

    def __init__(self, label: str):
        self.label = label
        self.active = False
        self.total_damage = 0
        self.swings = 0
        self.landed = 0
        self.crits = 0
        self.start_time: float | None = None
        self.last_activity_time: float | None = None
        self.last_broadcast = 0.0

    def _ensure_active(self, now: float):
        if not self.active:
            self.active = True
            self.start_time = now
            self.total_damage = 0
            self.swings = 0
            self.landed = 0
            self.crits = 0

    def record_damage(self, amount: int, now: float):
        self._ensure_active(now)
        self.total_damage += amount
        self.last_activity_time = now

    def record_swing(self, outcome: str | None, now: float):
        """Called for every melee event from you, hit or miss alike, so
        hit%/crit% reflect accuracy rather than just damage-dealing hits."""
        self._ensure_active(now)
        self.swings += 1
        if outcome in ("hit", "crit"):
            self.landed += 1
            if outcome == "crit":
                self.crits += 1
        self.last_activity_time = now

    def check_timeout(self, now: float) -> bool:
        """Returns True exactly on the active->paused transition."""
        if self.active and now - self.last_activity_time > COMBAT_TIMEOUT_SECONDS:
            self.active = False
            return True
        return False

    def snapshot(self, now: float) -> dict | None:
        if self.start_time is None:
            return None
        end = now if self.active else self.last_activity_time
        elapsed = max(end - self.start_time, 0.1)
        return {
            "kind": "dps",
            "label": self.label,
            "state": "active" if self.active else "paused",
            "damage": self.total_damage,
            "elapsed": round(elapsed, 1),
            "dps": round(self.total_damage / elapsed, 1),
            "hit_pct": round(self.landed / self.swings * 100, 1) if self.swings else None,
            "crit_pct": round(self.crits / self.landed * 100, 1) if self.landed else None,
        }


class OverlayBroadcaster:
    def __init__(self):
        self.clients: set[asyncio.StreamWriter] = set()
        self.server = None

    async def start(self, socket_path: str):
        if os.path.exists(socket_path):
            os.remove(socket_path)
        self.server = await asyncio.start_unix_server(self._handle_client, path=socket_path)

    async def _handle_client(self, reader, writer):
        self.clients.add(writer)
        try:
            while not reader.at_eof():
                await reader.read(1024)  # overlay clients are receive-only; drain any input
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    def send(self, obj: dict):
        data = (json.dumps(obj, default=str) + "\n").encode("utf-8")
        for writer in list(self.clients):
            try:
                writer.write(data)
            except (ConnectionResetError, BrokenPipeError):
                self.clients.discard(writer)


async def tail_log_source(conn, log_source, broadcaster: OverlayBroadcaster):
    game_code = _game_code_for(conn, log_source["game_id"])
    character_name = _character_name_for(conn, log_source["character_id"])
    parser_cls = get_parser(game_code)
    alert_engine = AlertEngine(conn, log_source["game_id"], broadcast=broadcaster.send)
    dps = DPSTracker(label=f"{character_name} ({game_code})")
    rare_tagger = ingest.RareGatherTagger()

    path = log_source["file_path"]
    log_source_id = log_source["id"]
    game_id = log_source["game_id"]
    character_id = log_source["character_id"]
    offset = log_source["last_byte_offset"]

    with open(path, "rb") as f:
        prefix = f.read(offset)
        line_no = prefix.count(b"\n")
        f.seek(offset)

        print(f"[tailer] watching {path} from byte {offset} (line {line_no})")
        while True:
            now = time.monotonic()
            # DPS bookkeeping runs every iteration -- including idle ones --
            # so the timeout fires and the on-screen timer keeps ticking
            # even during gaps between hits, not just when a new line lands.
            if dps.check_timeout(now):
                broadcaster.send(dps.snapshot(now))
            elif dps.active and now - dps.last_broadcast >= DPS_TICK_INTERVAL:
                broadcaster.send(dps.snapshot(now))
                dps.last_broadcast = now

            pos_before = f.tell()
            line_bytes = f.readline()
            if not line_bytes or not line_bytes.endswith(b"\n"):
                # partial line (still being written) or EOF -- rewind and wait
                f.seek(pos_before)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            offset += len(line_bytes)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            event = rare_tagger.apply(parser_cls.parse_line(line, line_no=line_no))

            events_batch = [(0, event)] if event is not None else []
            raw_batch = [(0, line_no, event.ts if event else None, line)]
            ingest.insert_batch(conn, game_id, character_id, log_source_id, events_batch, raw_batch)
            db.update_log_source_offset(conn, log_source_id, offset)
            line_no += 1

            if event is not None:
                alert_engine.evaluate(event)

                if event.event_type == "zone_change" and event.source_type == "you":
                    broadcaster.send({
                        "kind": "zone",
                        "label": f"{character_name} ({game_code})",
                        "zone": event.target_name,
                    })

                if event.source_type == "you":
                    activity = False
                    if event.event_type == "melee":
                        dps.record_swing(event.outcome, now)
                        activity = True
                        if event.amount is not None:
                            dps.record_damage(event.amount, now)
                    elif event.event_type in ("spell_damage", "ability_damage") and event.amount is not None:
                        dps.record_damage(event.amount, now)
                        activity = True
                    if activity:
                        broadcaster.send(dps.snapshot(now))
                        dps.last_broadcast = now


def _game_code_for(conn, game_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM games WHERE id=%s", (game_id,))
        return cur.fetchone()["code"]


def _character_name_for(conn, character_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM characters WHERE id=%s", (character_id,))
        return cur.fetchone()["name"]


async def discovery_loop(eql_root, eq2_root, broadcaster, running_log_source_ids, rescan_event: asyncio.Event):
    """Periodically re-scans the EQL/EQ2 log folders for new or rotated
    files (a new character, a new server, or the game creating a fresh file
    via a /log toggle) and starts tailing them without a restart. Also wakes
    up immediately (instead of waiting for the next timer tick) whenever
    rescan_event is set -- see trigger_rescan()/SIGUSR1 handling below."""
    while True:
        try:
            await asyncio.wait_for(rescan_event.wait(), timeout=DISCOVERY_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        rescan_event.clear()

        try:
            new_sources = discovery.scan_and_import(eql_root, eq2_root)
        except Exception as e:
            print(f"[tailer] discovery scan failed: {type(e).__name__}: {e}")
            continue
        for log_source in new_sources:
            if log_source["id"] in running_log_source_ids:
                continue
            print(f"[tailer] discovered new log source: {log_source['file_path']}")
            running_log_source_ids.add(log_source["id"])
            task_conn = db.get_connection()
            asyncio.create_task(tail_log_source(task_conn, log_source, broadcaster))


async def main_async(socket_path: str, eql_root: str | None, eq2_root: str | None):
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))

    rescan_event = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, rescan_event.set)

    if eql_root or eq2_root:
        print("[tailer] scanning for new/rotated log files...")
        discovery.scan_and_import(eql_root or "", eq2_root or "")

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM log_sources WHERE live=1")
        live_sources = cur.fetchall()

    if not live_sources:
        print(
            "No log_sources marked live=1 and none discovered. Run the importer with "
            "--live on a file first, or check log_roots in config/local.yaml."
        )
        return

    broadcaster = OverlayBroadcaster()
    await broadcaster.start(socket_path)
    print(f"[tailer] overlay socket listening at {socket_path}")

    # Each tailed file gets its own connection -- MySQL connections aren't
    # safe to share across concurrent asyncio tasks doing blocking I/O.
    running_log_source_ids = {ls["id"] for ls in live_sources}
    tasks = []
    for log_source in live_sources:
        task_conn = db.get_connection()
        tasks.append(asyncio.create_task(tail_log_source(task_conn, log_source, broadcaster)))

    if eql_root or eq2_root:
        tasks.append(asyncio.create_task(
            discovery_loop(eql_root or "", eq2_root or "", broadcaster, running_log_source_ids, rescan_event)
        ))

    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(description="Live-tail all log_sources marked live=1.")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    args = ap.parse_args()
    log_roots = db.config().get("log_roots", {})
    asyncio.run(main_async(args.socket, log_roots.get("eql"), log_roots.get("eq2")))


if __name__ == "__main__":
    main()
