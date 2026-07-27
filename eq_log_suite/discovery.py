"""Auto-discovers new/rotated EQ-family log files (a new character-server
combo, or EQ2 creating a fresh file via a /log toggle) and starts tracking
them automatically -- imports the file and marks it live, with no manual
`importer.py --live` step needed. Called once at tailer startup and on a
recurring interval while it runs (see tailer.py).

Only one log_source should be "live" per character at a time. Which one
that is gets resolved by file mtime (the actual signal for "which file is
the game currently writing to"), as a pass over all of a character's known
files -- not by "whichever was imported most recently in this scan", which
breaks when several new files for the same character show up in one pass
(they'd cascade-demote each other in scan order instead of picking the one
that's actually active).
"""

import os
import re
from pathlib import Path

from eq_log_suite import db
from eq_log_suite.importer import guess_character_from_filename, guess_server_from_filename

EQLOG_NAME_RE = re.compile(r"^eqlog_.*\.txt$", re.IGNORECASE)
# EQ2's default log filename (bare "/log" with no custom name) embeds the
# character directly, same idea as EQL's eqlog_<Character>_<Server>.txt --
# e.g. "eq2log_Cheerfulness.txt". A custom "/log <name>" (e.g. "testers.txt")
# doesn't encode a character, so falls back to the same-server-folder guess.
EQ2LOG_DEFAULT_NAME_RE = re.compile(r"^eq2log_(?P<character>.+)\.txt$", re.IGNORECASE)


def _discover_eql(root: str) -> list[str]:
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    return [str(p) for p in sorted(root_path.glob("*.txt")) if EQLOG_NAME_RE.match(p.name)]


def _discover_eq(root: str) -> list[str]:
    # Same eqlog_<Character>_<Server>.txt naming as EQL -- both are
    # EQ-client-derived, see eq_log_suite/parsers/eq.py.
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    return [str(p) for p in sorted(root_path.glob("*.txt")) if EQLOG_NAME_RE.match(p.name)]


def _discover_eq2(root: str) -> list[str]:
    root_path = Path(root)
    if not root_path.is_dir():
        return []
    return [str(p) for p in sorted(root_path.rglob("*.txt"))]


def _already_tracked(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM log_sources")
        return {row["file_path"] for row in cur.fetchall()}


def _existing_eq2_character_for_server(conn, server: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.name FROM characters c JOIN games g ON c.game_id=g.id "
            "WHERE g.code='eq2' AND c.server=%s LIMIT 1",
            (server,),
        )
        row = cur.fetchone()
        return row["name"] if row else None


def _import_new_file(path: str, game_code: str, character_name: str, server: str | None) -> dict:
    """Imports a newly-discovered file (not yet resolved for live status --
    see _resolve_live_status) and returns its log_sources row."""
    from eq_log_suite.importer import import_file  # local import: avoids a circular import with tailer

    import_file(path, game_code=game_code, character_name=character_name, server=server, mark_live=False)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_sources WHERE file_path=%s", (path,))
            return cur.fetchone()
    finally:
        conn.close()


def _resolve_live_status(character_ids: set[int]) -> None:
    """For each given character, marks live=1 on whichever of its known log
    files has the most recent mtime (the one actually being written to right
    now) and live=0 on all its others."""
    if not character_ids:
        return
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            for character_id in character_ids:
                cur.execute("SELECT * FROM log_sources WHERE character_id=%s", (character_id,))
                sources = cur.fetchall()
                if not sources:
                    continue
                by_mtime = sorted(
                    sources,
                    key=lambda s: os.path.getmtime(s["file_path"]) if os.path.exists(s["file_path"]) else -1,
                    reverse=True,
                )
                most_recent = by_mtime[0]
                for s in sources:
                    live = 1 if s["id"] == most_recent["id"] else 0
                    if s["live"] != live:
                        cur.execute("UPDATE log_sources SET live=%s WHERE id=%s", (live, s["id"]))
        conn.commit()
    finally:
        conn.close()


def scan_and_import(eql_root: str, eq2_root: str, eq_root: str = "") -> list[dict]:
    """Returns the (possibly now-live) log_sources rows for any
    newly-discovered files."""
    conn = db.get_connection()
    try:
        tracked = _already_tracked(conn)
    finally:
        conn.close()

    newly_added = []
    touched_character_ids: set[int] = set()

    for path in _discover_eql(eql_root):
        if path in tracked:
            continue
        character = guess_character_from_filename(path) or "Unknown"
        server = guess_server_from_filename(path)
        row = _import_new_file(path, "eql", character, server)
        newly_added.append(row)
        touched_character_ids.add(row["character_id"])

    for path in _discover_eq(eq_root):
        if path in tracked:
            continue
        character = guess_character_from_filename(path) or "Unknown"
        server = guess_server_from_filename(path)
        row = _import_new_file(path, "eq", character, server)
        newly_added.append(row)
        touched_character_ids.add(row["character_id"])

    for path in _discover_eq2(eq2_root):
        if path in tracked:
            continue
        server = Path(path).parent.name  # server-named subfolder, e.g. "Halls of Fate"
        name_match = EQ2LOG_DEFAULT_NAME_RE.match(Path(path).name)
        if name_match:
            character = name_match.group("character")
        else:
            # a custom "/log <name>" file (e.g. "testers.txt") doesn't encode
            # a character -- assume whichever character is already known on
            # this same server folder.
            conn2 = db.get_connection()
            try:
                character = _existing_eq2_character_for_server(conn2, server) or "Unknown"
            finally:
                conn2.close()
        row = _import_new_file(path, "eq2", character, server)
        newly_added.append(row)
        touched_character_ids.add(row["character_id"])

    _resolve_live_status(touched_character_ids)

    if not newly_added:
        return []
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            ids = [row["id"] for row in newly_added]
            placeholders = ",".join(["%s"] * len(ids))
            cur.execute(f"SELECT * FROM log_sources WHERE id IN ({placeholders})", ids)
            return cur.fetchall()
    finally:
        conn.close()
