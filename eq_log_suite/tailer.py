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
import calendar
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Log rotation, to keep an actively-tailed eqlog file from growing unbounded
# over a long-running character. EQ doesn't hold the log file open for the
# whole session -- it notices the path disappear and starts writing a fresh
# file there (or picks up an existing empty one immediately) on its own, so
# renaming the file aside and recreating an empty one at the original path
# is safe to do live, not just while EQ is closed. Policy (whether to
# auto-rotate at all, and how) is per-game -- see db.get_rotation_settings --
# and configurable from the home page; mode='manual' (the default for a game
# that's never been configured) means no automatic schedule at all, only the
# "Rotate now" button (which sets manual_trigger_at, honored regardless of
# mode).
#
# Rather than scheduling a single "next rotation" moment (which would miss
# entirely if the tailer wasn't running at that exact instant), each
# log_source persists last_rotated_at (see db.mark_log_source_rotated) and
# every loop tick asks "has a rotation boundary passed since we last
# rotated?" via _most_recent_weekday_boundary/_most_recent_month_day_boundary.
# That covers all of: rotating right on time while running through the
# boundary, rotating on schedule even while idle (no new lines, loop still
# ticks), and catching up immediately on startup if the tailer was off when
# the boundary passed -- whether that was five minutes ago or two weeks ago
# (e.g. back from vacation) -- instead of waiting for the next one.
ROTATION_TZ = ZoneInfo("America/New_York")
ROTATION_SETTINGS_REFRESH_SECONDS = 15  # how often each tail task re-reads its game's policy


def _most_recent_weekday_boundary(before: datetime, weekday: int, hour: int) -> datetime:
    """The most recent `weekday` (0=Monday..6=Sunday) at `hour`:00 in
    ROTATION_TZ that is <= `before` (which must be tz-aware)."""
    local = before.astimezone(ROTATION_TZ)
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    days_back = (candidate.weekday() - weekday) % 7
    candidate -= timedelta(days=days_back)
    if candidate > local:
        candidate -= timedelta(weeks=1)
    return candidate


def _most_recent_month_day_boundary(before: datetime, day_of_month: int, hour: int) -> datetime:
    """The most recent `day_of_month` (1-31, capped to the last real day of
    a short month -- e.g. 31 means "last day" in a 30-day month) at
    `hour`:00 in ROTATION_TZ that is <= `before`."""
    local = before.astimezone(ROTATION_TZ)

    def boundary_for(year: int, month: int) -> datetime:
        day = min(day_of_month, calendar.monthrange(year, month)[1])
        return datetime(year, month, day, hour, 0, 0, tzinfo=ROTATION_TZ)

    candidate = boundary_for(local.year, local.month)
    if candidate > local:
        year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
        candidate = boundary_for(year, month)
    return candidate


def _rotation_due(settings: dict, wall_now: datetime, last_rotated_at: datetime | None, current_size: int) -> bool:
    """True if `settings` (a rotation_settings row, or db.get_rotation_settings's
    manual-only default) calls for a rotation right now. The manual trigger
    (the home page's "Rotate now" button) always takes priority and applies
    regardless of mode."""
    manual_trigger_at = settings.get("manual_trigger_at")
    if manual_trigger_at is not None:
        trigger = manual_trigger_at.replace(tzinfo=timezone.utc)
        if last_rotated_at is None or last_rotated_at < trigger:
            return True

    mode = settings.get("mode", "manual")
    if mode == "day_of_week" and settings.get("day_of_week") is not None:
        boundary = _most_recent_weekday_boundary(wall_now, settings["day_of_week"], settings["hour"])
        return last_rotated_at is None or last_rotated_at < boundary
    if mode == "day_of_month" and settings.get("day_of_month") is not None:
        boundary = _most_recent_month_day_boundary(wall_now, settings["day_of_month"], settings["hour"])
        return last_rotated_at is None or last_rotated_at < boundary
    if mode == "size" and settings.get("size_bytes"):
        return current_size >= settings["size_bytes"]
    return False  # mode == "manual" (or an unconfigured mode field): no automatic schedule


def _rotate_log_file(path: str) -> int:
    """Renames the current log file aside with a timestamp suffix, then
    immediately creates an empty file at the original path -- back to back,
    no I/O in between, to keep the window where the path doesn't exist as
    close to zero as possible. Returns the new byte offset (always 0)."""
    archive_path = f"{path}.{datetime.now(ROTATION_TZ):%Y%m%d-%H%M%S}"
    os.rename(path, archive_path)
    open(path, "wb").close()
    print(f"[tailer] rotated {path} -> {archive_path}")
    return 0

# DPS meter tuning: how it decides combat has ended (freeze, don't reset to
# zero) vs. is still ongoing between hits, and how often it re-broadcasts a
# still-active encounter so the on-screen timer visibly keeps ticking even
# between hits.
COMBAT_TIMEOUT_SECONDS = 6.0
DPS_TICK_INTERVAL = 0.5
# How long an in-combat/out-of-combat alert stays on screen before the
# overlay/MangoHud clear it -- separate from COMBAT_TIMEOUT_SECONDS, which
# only decides when combat itself is considered over.
COMBAT_ALERT_DURATION_SECONDS = 10.0
# Off by default -- was useful to confirm CombatTracker/DPSTracker work
# correctly, but the user doesn't want a dedicated in/out-of-combat box
# any more now that fixed, user-assignable alert boxes exist (see
# eq_log_suite/overlay/boxes.py). Flip back to True to re-enable.
COMBAT_STATE_ALERTS_ENABLED = False


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

    def force_start(self, now: float):
        """Forces a fresh window to begin exactly at `now`, discarding
        whatever was previously tracked -- unlike _ensure_active (which only
        resets if not already active), this always resets. Driven by an
        explicit "Encounter Start" /say marker (see h_encounter_marker) so a
        dummy/gear-testing pull gets its own clean window regardless of
        whatever combat state preceded it."""
        self.active = True
        self.start_time = now
        self.total_damage = 0
        self.swings = 0
        self.landed = 0
        self.crits = 0
        self.last_activity_time = now

    def force_stop(self, now: float) -> dict | None:
        """Freezes the window exactly at `now`, rather than waiting for the
        natural COMBAT_TIMEOUT_SECONDS gap, and returns its final snapshot.
        Driven by an explicit "Encounter Stop" /say marker."""
        if self.start_time is None:
            return None
        self.active = False
        self.last_activity_time = now
        return self.snapshot(now)

    def snapshot(self, now: float) -> dict | None:
        if self.start_time is None:
            return None
        end = now if self.active else self.last_activity_time
        elapsed = max(end - self.start_time, 0.1)
        return {
            "kind": "dps",
            "key": "dps_meter",
            "label": self.label,
            "state": "active" if self.active else "paused",
            "damage": self.total_damage,
            "elapsed": round(elapsed, 1),
            "dps": round(self.total_damage / elapsed, 1),
            "hit_pct": round(self.landed / self.swings * 100, 1) if self.swings else None,
            "crit_pct": round(self.crits / self.landed * 100, 1) if self.landed else None,
        }


class CombatTracker:
    """Tracks distinct mobs you've swung at recently, to broadcast an
    in-combat/out-of-combat alert with a live "how many mobs" count --
    same COMBAT_TIMEOUT_SECONDS window DPSTracker uses to decide combat
    has ended, but keyed per-mob rather than a single active/paused flag.
    """

    def __init__(self, label: str):
        self.label = label
        self.mobs: dict[str, float] = {}  # target_name -> last swing time

    def record(self, target_name: str, now: float) -> bool:
        """Adds/refreshes a mob's activity timestamp. Returns True whenever
        target_name is newly added -- covers both the first hit after being
        fully out of combat and a new mob joining an already-ongoing fight."""
        is_new = target_name not in self.mobs
        self.mobs[target_name] = now
        return is_new

    def prune(self, now: float) -> bool:
        """Drops mobs idle past the combat timeout. Returns True exactly on
        the non-empty -> empty transition (all mobs disengaged)."""
        was_active = bool(self.mobs)
        stale = [name for name, t in self.mobs.items() if now - t > COMBAT_TIMEOUT_SECONDS]
        for name in stale:
            del self.mobs[name]
        return was_active and not self.mobs

    @property
    def count(self) -> int:
        return len(self.mobs)


class MobDamageTracker:
    """Tracks cumulative damage taken by each currently-engaged mob, from
    any source -- you, another party member, or (per the same classify_actor
    ambiguity CombatTracker/DPSTracker's party numbers already accept) a
    pet or unique-named NPC ally. "The mob you're hitting" is just whichever
    target you personally dealt damage to most recently; its running total
    is everyone's damage against that specific name, not a fight-wide sum
    across every mob in an AoE pull.
    """

    def __init__(self):
        self.damage: dict[str, int] = {}
        self.last_hit: dict[str, float] = {}
        self.current_target: str | None = None

    def record(self, target_name: str, amount: int, now: float, *, is_self: bool):
        self.damage[target_name] = self.damage.get(target_name, 0) + amount
        self.last_hit[target_name] = now
        if is_self:
            self.current_target = target_name

    def clear(self, target_name: str):
        """Drops a mob's tally -- called on its death so the next mob with
        the same name (a fresh trash spawn) starts from zero, not wherever
        the last one's health bar left off."""
        self.damage.pop(target_name, None)
        self.last_hit.pop(target_name, None)
        if self.current_target == target_name:
            self.current_target = None

    def prune(self, now: float):
        stale = [name for name, t in self.last_hit.items() if now - t > COMBAT_TIMEOUT_SECONDS]
        for name in stale:
            self.clear(name)

    def current(self) -> tuple[str, int] | None:
        if self.current_target is None:
            return None
        return self.current_target, self.damage.get(self.current_target, 0)


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
    combat = CombatTracker(label=f"{character_name} ({game_code})")
    mob_damage = MobDamageTracker()

    def snapshot_with_target(t: float) -> dict | None:
        """DPSTracker.snapshot() plus whichever mob you're currently hitting
        and its total damage taken from all sources -- merged at the call
        site rather than inside DPSTracker itself since target tracking is
        keyed by mob name, not by character."""
        msg = dps.snapshot(t)
        if msg is not None:
            current = mob_damage.current()
            if current is not None:
                msg["target_name"], msg["target_damage"] = current
        return msg
    # The lookups above are all bare SELECTs with autocommit off, so without
    # this the transaction they opened just sits there -- holding a metadata
    # lock on games/characters/alert_rules -- for as long as the game stays
    # down and no log line comes in to trigger a commit elsewhere. Blocked a
    # schema migration this way once already.
    conn.commit()

    path = log_source["file_path"]
    log_source_id = log_source["id"]
    game_id = log_source["game_id"]
    character_id = log_source["character_id"]
    offset = log_source["last_byte_offset"]
    last_rotated_at = log_source.get("last_rotated_at")
    if last_rotated_at is not None:
        last_rotated_at = last_rotated_at.replace(tzinfo=timezone.utc)

    rotation_settings = db.get_rotation_settings(conn, game_id)
    rotation_settings_refreshed_at = time.monotonic()

    f = open(path, "rb")
    try:
        prefix = f.read(offset)
        line_no = prefix.count(b"\n")
        f.seek(offset)

        print(f"[tailer] watching {path} from byte {offset} (line {line_no})")
        while True:
            now = time.monotonic()
            if now - rotation_settings_refreshed_at > ROTATION_SETTINGS_REFRESH_SECONDS:
                rotation_settings = db.get_rotation_settings(conn, game_id)
                rotation_settings_refreshed_at = now

            wall_now = datetime.now(ROTATION_TZ)
            current_size = os.fstat(f.fileno()).st_size if rotation_settings.get("mode") == "size" else 0
            if _rotation_due(rotation_settings, wall_now, last_rotated_at, current_size):
                f.close()
                offset = _rotate_log_file(path)
                f = open(path, "rb")
                last_rotated_at = wall_now
                db.mark_log_source_rotated(conn, log_source_id, offset)

            mob_damage.prune(now)

            # DPS bookkeeping runs every iteration -- including idle ones --
            # so the timeout fires and the on-screen timer keeps ticking
            # even during gaps between hits, not just when a new line lands.
            if dps.check_timeout(now):
                broadcaster.send(snapshot_with_target(now))
            elif dps.active and now - dps.last_broadcast >= DPS_TICK_INTERVAL:
                broadcaster.send(snapshot_with_target(now))
                dps.last_broadcast = now

            if combat.prune(now) and COMBAT_STATE_ALERTS_ENABLED:
                broadcaster.send({
                    "kind": "alert",
                    "key": "combat_state",
                    "text": "Out of Combat",
                    "color": [0.55, 0.55, 0.55],
                    "duration": COMBAT_ALERT_DURATION_SECONDS,
                })

            pos_before = f.tell()
            line_bytes = f.readline()
            if not line_bytes or not line_bytes.endswith(b"\n"):
                # partial line (still being written) or EOF -- rewind and wait
                f.seek(pos_before)
                await asyncio.sleep(POLL_INTERVAL)
                continue

            offset += len(line_bytes)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            event = parser_cls.parse_line(line, line_no=line_no)

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

                if event.event_type == "encounter_marker" and event.source_type == "you":
                    # "Encounter Start"/"Encounter Stop" /say markers -- same
                    # concept as the server-side hard-start/hard-stop rows
                    # (see _derive_gap_based_encounters), applied to the live
                    # DPS meter too: freezes duration/damage exactly at the
                    # marker's own timestamp instead of the usual
                    # COMBAT_TIMEOUT_SECONDS gap, for dummy/gear-testing.
                    if event.verb == "start":
                        dps.force_start(now)
                        broadcaster.send(snapshot_with_target(now))
                        dps.last_broadcast = now
                    elif event.verb == "stop":
                        snap = dps.force_stop(now)
                        if snap:
                            broadcaster.send(snap)
                            dps.last_broadcast = now

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
                    if (event.event_type in ("melee", "spell_damage", "ability_damage")
                            and event.target_name and event.amount is not None):
                        mob_damage.record(event.target_name, event.amount, now, is_self=True)
                    if activity:
                        broadcaster.send(snapshot_with_target(now))
                        dps.last_broadcast = now

                    if event.event_type in ("melee", "spell_damage", "ability_damage") and event.target_name:
                        if combat.record(event.target_name, now) and COMBAT_STATE_ALERTS_ENABLED:
                            mob_word = "mob" if combat.count == 1 else "mobs"
                            broadcaster.send({
                                "kind": "alert",
                                "key": "combat_state",
                                "text": f"In Combat ({combat.count} {mob_word})",
                                "color": [0.9, 0.3, 0.2],
                                "duration": COMBAT_ALERT_DURATION_SECONDS,
                            })

                elif event.source_type == "unknown" and event.target_type == "npc":
                    # Someone/something else (party member, pet, or a
                    # unique-named NPC ally -- see classify_actor, log text
                    # can't tell these apart) hitting a mob. Only feeds
                    # mob_damage's per-target total, not your own DPS/hit/
                    # crit numbers -- those stay yours alone.
                    if (event.event_type in ("melee", "spell_damage", "ability_damage")
                            and event.target_name and event.amount is not None):
                        mob_damage.record(event.target_name, event.amount, now, is_self=False)
                        if dps.active:
                            broadcaster.send(snapshot_with_target(now))

                if event.event_type == "death" and event.target_type == "npc" and event.target_name:
                    # Mob's gone -- clear its tally so the next same-named
                    # trash spawn starts from zero instead of inheriting
                    # whatever health the last one had lost.
                    mob_damage.clear(event.target_name)
    finally:
        f.close()


def _game_code_for(conn, game_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM games WHERE id=%s", (game_id,))
        return cur.fetchone()["code"]


def _character_name_for(conn, character_id: int) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM characters WHERE id=%s", (character_id,))
        return cur.fetchone()["name"]


async def discovery_loop(eql_root, eq_root, broadcaster, running_log_source_ids, rescan_event: asyncio.Event):
    """Periodically re-scans the EQL/EQ log folders for new or rotated files
    (a new character or a new server) and starts tailing them without a
    restart. Also wakes up immediately (instead of waiting for the next
    timer tick) whenever rescan_event is set -- see trigger_rescan()/SIGUSR1
    handling below."""
    while True:
        try:
            await asyncio.wait_for(rescan_event.wait(), timeout=DISCOVERY_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        rescan_event.clear()

        try:
            new_sources = discovery.scan_and_import(eql_root, eq_root)
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


async def main_async(socket_path: str, eql_root: str | None, eq_root: str | None = None):
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()))

    rescan_event = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, rescan_event.set)

    if eql_root or eq_root:
        print("[tailer] scanning for new/rotated log files...")
        discovery.scan_and_import(eql_root or "", eq_root or "")

    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM log_sources WHERE live=1")
        live_sources = cur.fetchall()
    # This connection isn't reused for anything else -- each tail task below
    # gets its own -- so end its transaction and hand it back to the pool
    # rather than letting it sit open (and holding a metadata lock) for the
    # rest of this forever-running coroutine's life.
    conn.commit()
    conn.close()

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

    if eql_root or eq_root:
        tasks.append(asyncio.create_task(
            discovery_loop(eql_root or "", eq_root or "", broadcaster, running_log_source_ids, rescan_event)
        ))

    await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(description="Live-tail all log_sources marked live=1.")
    ap.add_argument("--socket", default=DEFAULT_SOCKET_PATH)
    args = ap.parse_args()
    log_roots = db.config().get("log_roots", {})
    asyncio.run(main_async(args.socket, log_roots.get("eql"), log_roots.get("eq")))


if __name__ == "__main__":
    main()
