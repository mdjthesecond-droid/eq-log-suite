import bisect
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eq_log_suite import db
from eq_log_suite.parsers.base import GameParser

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent

app = FastAPI(title="EQ Log Suite")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Zone correlation for an event `ev`: the most recent zone_change logged
# before it. Falls back to zone_start_overrides (a one-time, per-character,
# user-confirmed answer -- see schema.sql) for the leading gap before a
# character's first-ever logged zone_change, where there's nothing to
# correlate against. Used identically by /loot and /quests.
_ZONE_LOOKUP_EXPR = (
    "COALESCE("
    "(SELECT z.target_name FROM events z WHERE z.event_type='zone_change' "
    "AND z.character_id=ev.character_id AND z.ts<=ev.ts ORDER BY z.ts DESC LIMIT 1), "
    "(SELECT zso.zone FROM zone_start_overrides zso WHERE zso.character_id=ev.character_id))"
)


def _unresolved_zone_starts(game):
    """Characters (in this game) with activity that predates their first-ever
    logged zone_change (or have no zone_change logged at all yet) and don't
    already have a zone_start_overrides entry -- i.e. need a human to confirm
    what zone they were actually in before the log record begins."""
    sql = (
        "SELECT c.id AS character_id, c.name AS character_name, MIN(ev.ts) AS earliest_ts "
        "FROM events ev "
        "JOIN characters c ON ev.character_id = c.id "
        "JOIN games g ON c.game_id = g.id "
        "LEFT JOIN zone_start_overrides zso ON zso.character_id = c.id "
        "WHERE g.code = %s AND zso.character_id IS NULL "
        "  AND ev.ts < COALESCE("
        "        (SELECT MIN(z.ts) FROM events z WHERE z.event_type='zone_change' AND z.character_id=c.id), "
        "        '9999-12-31'"
        "      ) "
        "GROUP BY c.id, c.name "
        "ORDER BY c.name"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (game,))
            return cur.fetchall()
    finally:
        conn.close()


@app.post("/zone-start/set")
def zone_start_set(character_id: int = Form(...), zone: str = Form(...), note: str = Form(""), next: str = Form("/")):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO zone_start_overrides (character_id, zone, note) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE zone=VALUES(zone), note=VALUES(note)",
                (character_id, zone, note or None),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(next, status_code=303)


def run_select(sql: str):
    stripped = sql.strip().rstrip(";")
    if not stripped.lower().startswith("select"):
        return [], [], "Only SELECT statements are allowed here."
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(stripped)
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description] if cur.description else []
        return rows, columns, ""
    except Exception as e:
        return [], [], str(e)
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.get("/eql", response_class=HTMLResponse)
def eql_hub(request: Request):
    return templates.TemplateResponse(request, "game_hub.html", {
        "game": "eql", "game_label": "EQ Legends",
    })


@app.get("/eq", response_class=HTMLResponse)
def eq_hub(request: Request):
    return templates.TemplateResponse(request, "game_hub.html", {
        "game": "eq", "game_label": "EverQuest",
    })


@app.get("/query", response_class=HTMLResponse)
def query_form(request: Request):
    return templates.TemplateResponse(request, "query.html", {"sql": "", "rows": [], "columns": [], "error": ""})


@app.post("/query", response_class=HTMLResponse)
def query_submit(request: Request, sql: str = Form(...)):
    rows, columns, error = run_select(sql)
    return templates.TemplateResponse(request, "query.html", {
        "sql": sql, "rows": rows, "columns": columns, "error": error,
    })


@app.get("/events", response_class=HTMLResponse)
def events_browse(
    request: Request, game: str = "", character: str = "", event_type: str = "",
    source: str = "", target: str = "", verb: str = "", outcome: str = "",
    since: str = "", until: str = "", limit: int = 500,
):
    clauses, params = [], []
    if game:
        clauses.append("g.code = %s"); params.append(game)
    if character:
        clauses.append("c.name = %s"); params.append(character)
    if event_type:
        clauses.append("e.event_type = %s"); params.append(event_type)
    if source:
        clauses.append("e.source_name LIKE %s"); params.append(f"%{source}%")
    if target:
        clauses.append("e.target_name LIKE %s"); params.append(f"%{target}%")
    if verb:
        clauses.append("e.verb LIKE %s"); params.append(f"%{verb}%")
    if outcome:
        clauses.append("e.outcome = %s"); params.append(outcome)
    if since:
        clauses.append("e.ts >= %s"); params.append(since)
    if until:
        clauses.append("e.ts <= %s"); params.append(until)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(limit, 5000))
    sql = (
        "SELECT e.id, e.ts, g.code AS game, c.name AS character_name, e.event_type, e.source_name, "
        "e.source_type, e.target_name, e.target_type, e.verb, e.amount, e.outcome, e.raw_line "
        "FROM events e JOIN games g ON e.game_id=g.id JOIN characters c ON e.character_id=c.id "
        f"{where} ORDER BY e.ts DESC LIMIT {limit}"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    columns = list(rows[0].keys()) if rows else [
        "id", "ts", "game", "character_name", "event_type", "source_name", "source_type",
        "target_name", "target_type", "verb", "amount", "outcome", "raw_line",
    ]

    filled_sql = sql
    for p in params:
        filled_sql = filled_sql.replace("%s", repr(p), 1)

    return templates.TemplateResponse(request, "events.html", {
        "rows": rows, "columns": columns, "sql": filled_sql,
        "filters": {
            "game": game, "character": character, "event_type": event_type, "source": source,
            "target": target, "verb": verb, "outcome": outcome, "since": since, "until": until,
            "limit": limit,
        },
    })


LOOT_RESULT_LIMIT = 50


def _compute_loot(game, npc="", item="", zone=""):
    # /loot used to render the *entire* zone->npc->item drop table
    # unconditionally (~0.7s of correlated zone lookups over every loot/
    # death event, every single page load) now that /zoneinfo covers
    # per-zone browsing and each NPC's own page covers per-npc browsing.
    # It now always runs (see the two routes below), capped at
    # LOOT_RESULT_LIMIT rows -- but that cap is applied only after grouping,
    # so it doesn't bound the real cost driver: correlating each loot/kill
    # row to "whichever zone_change happened most recently before it".
    # That correlation used to be _base_zone_expr(), a SQL correlated
    # subquery evaluated once per row -- fine at a few thousand rows, but
    # confirmed real (2026-08-13): with ~8400 loot events + ~3100 kills
    # after a loot-parser backfill added ~1700 rows, /loot took 86s to
    # load. Same root cause as [[feedback-correlated-subquery-per-row-not-per-group]]
    # (MariaDB doesn't materialize/index this well per-row), so it gets the
    # same fix already used elsewhere in this file (see the con/death
    # tier-lookup a few hundred lines down): fetch the small zone_change
    # table once per character and do the "most recent prior row" lookup
    # in Python with bisect instead of in SQL.
    #
    # Future extension point: once items have a curated type (trash/quest/
    # tradeskill/weapon slot+type/armor slot+type -- not built yet), that'd
    # join in here (e.g. an `item_info` table keyed on item name, same
    # shape as npc_info/zone_info) and surface as a filter/column alongside
    # npc/item/zone.
    loot_clauses, loot_params = ["ev.event_type='loot'", "g.code = %s"], [game]
    if npc:
        loot_clauses.append("ev.source_name LIKE %s"); loot_params.append(f"%{npc}%")
    if item:
        loot_clauses.append("ev.target_name LIKE %s"); loot_params.append(f"%{item}%")

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.source_name AS npc, "
                "ev.target_name AS item, ev.amount AS qty "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE {' AND '.join(loot_clauses)}",
                loot_params,
            )
            loot_rows = cur.fetchall()

            # Drop chance needs kills as the denominator -- not every kill
            # drops anything, so "how often does X drop" only means
            # something relative to "how many times did I kill it". Not
            # narrowed to loot_rows' npcs here (that filtering happened in
            # SQL before, as a cheap `IN`) -- with only ~3000 rows total,
            # fetching all of them and letting the dict lookups below
            # simply go unused for npcs with no loot match is simpler and
            # not meaningfully slower.
            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.target_name AS npc "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='death' AND ev.source_type='you' AND g.code=%s",
                (game,),
            )
            kill_rows = cur.fetchall()

            cur.execute(
                "SELECT ev.character_id, ev.ts, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.base_zone')) AS zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='zone_change' AND g.code=%s ORDER BY ev.character_id, ev.ts",
                (game,),
            )
            zone_changes = cur.fetchall()
    finally:
        conn.close()

    # Same "most recent zone_change at or before ts, per character" lookup
    # as _base_zone_expr, done once here instead of as a correlated SQL
    # subquery -- see the comment above for why.
    zc_by_char: dict = {}
    for r in zone_changes:
        zc = zc_by_char.setdefault(r["character_id"], {"ts": [], "zone": []})
        zc["ts"].append(r["ts"])
        zc["zone"].append(r["zone"])

    def zone_at(character_id, ts):
        zc = zc_by_char.get(character_id)
        if not zc:
            return None
        i = bisect.bisect_right(zc["ts"], ts) - 1
        return zc["zone"][i] if i >= 0 else None

    for r in loot_rows:
        r["zone"] = zone_at(r["character_id"], r["ts"])
    for r in kill_rows:
        r["zone"] = zone_at(r["character_id"], r["ts"])

    loot_grouped: dict = {}
    for r in loot_rows:
        key = (r["npc"], r["zone"], r["item"])
        g = loot_grouped.setdefault(key, {"qtys": [], "drops": 0})
        g["qtys"].append(r["qty"] if r["qty"] is not None else 1)
        g["drops"] += 1

    kill_counts: dict = {}
    for r in kill_rows:
        key = (r["npc"], r["zone"])
        kill_counts[key] = kill_counts.get(key, 0) + 1

    rows = []
    for (npc_, zone_, item_), g in loot_grouped.items():
        kill_count = kill_counts.get((npc_, zone_))
        rows.append({
            "zone": zone_, "npc": npc_, "item": item_,
            "avg_qty": round(sum(g["qtys"]) / len(g["qtys"]), 1),
            "drops": g["drops"],
            "kill_count": kill_count,
            "chance_pct": round(g["drops"] / kill_count * 100, 2) if kill_count else None,
        })

    if zone:
        needle = zone.lower()
        rows = [r for r in rows if needle in (r["zone"] or "").lower()]

    # NULLs (no kill_count) sort last, same as MariaDB's DESC-puts-NULL-last
    # behavior in the SQL version this replaced.
    rows.sort(key=lambda r: (r["item"] or "", r["chance_pct"] is None, -(r["chance_pct"] or 0)))
    truncated = len(rows) > LOOT_RESULT_LIMIT
    rows = rows[:LOOT_RESULT_LIMIT]

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # Level (from /con) as appropriate, per npc -- same source as
            # each NPC's own page, just fetched once here rather than
            # correlated per loot row. Matched case-insensitively: classic
            # EQ's own text casing for the same mob name varies by message
            # template (con lines are sentence-initial, "A dry bone
            # skeleton"; loot/death lines are mid-sentence, "a dry bone
            # skeleton") -- confirmed real, not a parsing bug, so the join
            # has to tolerate it or the level would silently never show.
            levels_by_npc = {}
            if rows:
                cur.execute(
                    "SELECT ev.source_name AS npc, MIN(ev.amount) AS level_min, MAX(ev.amount) AS level_max "
                    "FROM events ev JOIN games g ON ev.game_id=g.id "
                    "WHERE ev.event_type='con' AND g.code=%s GROUP BY ev.source_name",
                    (game,),
                )
                levels_by_npc = {r["npc"].lower(): r for r in cur.fetchall()}
    finally:
        conn.close()

    # groupby() in the template re-sorts by item then zone -- None (a
    # kill/drop before any zone_change was ever logged) can't be ordered
    # against itself, so normalize to a sortable placeholder here rather
    # than at render time.
    for r in rows:
        r["zone"] = r["zone"] or "(unknown zone)"
        r["npc"] = r["npc"] or "(unknown npc)"
        lvl = levels_by_npc.get(r["npc"].lower())
        r["level"] = None
        if lvl and lvl["level_min"] is not None:
            r["level"] = (
                str(lvl["level_min"]) if lvl["level_min"] == lvl["level_max"]
                else f'{lvl["level_min"]}-{lvl["level_max"]}'
            )
    return rows, truncated


@app.get("/loot/eql", response_class=HTMLResponse)
def loot_report_eql(request: Request, npc: str = "", item: str = "", zone: str = ""):
    rows, truncated = _compute_loot("eql", npc, item, zone)
    return templates.TemplateResponse(request, "loot.html", {
        "rows": rows,
        "truncated": truncated,
        "game": "eql",
        "game_label": "EQ Legends",
        "unresolved_zone_starts": _unresolved_zone_starts("eql"),
        "filters": {"npc": npc, "item": item, "zone": zone},
    })


@app.get("/loot/eq", response_class=HTMLResponse)
def loot_report_eq(request: Request, npc: str = "", item: str = "", zone: str = ""):
    rows, truncated = _compute_loot("eq", npc, item, zone)
    return templates.TemplateResponse(request, "loot.html", {
        "rows": rows,
        "truncated": truncated,
        "game": "eq",
        "game_label": "EverQuest",
        "unresolved_zone_starts": _unresolved_zone_starts("eq"),
        "filters": {"npc": npc, "item": item, "zone": zone},
    })



def _compute_quests(game, character="", quest="", zone="", npc=""):
    inner_clauses, inner_params = ["g.code = %s"], [game]
    if character:
        inner_clauses.append("c.name = %s"); inner_params.append(character)
    inner_where = f"AND {' AND '.join(inner_clauses)}" if inner_clauses else ""

    outer_clauses, outer_params = [], []
    if quest:
        outer_clauses.append("quest LIKE %s"); outer_params.append(f"%{quest}%")
    if zone:
        outer_clauses.append("zone LIKE %s"); outer_params.append(f"%{zone}%")
    if npc:
        outer_clauses.append("npc LIKE %s"); outer_params.append(f"%{npc}%")
    outer_where = f"WHERE {' AND '.join(outer_clauses)}" if outer_clauses else ""

    sql = (
        # zone/npc aren't stated on the quest-completion line itself --
        # correlate against whichever zone_change/hail happened most
        # recently before it for that character (same technique /zones
        # already uses). npc is a best-effort "who was I probably
        # talking to" via the last NPC you hailed, not a guaranteed turn-in
        # target -- quest chains can involve hailing several NPCs before
        # the actual completion.
        "SELECT * FROM ("
        "  SELECT ev.ts, c.name AS character_name, g.code AS game, ev.target_name AS quest, "
        "    JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.reward')) AS reward, "
        f"    {_ZONE_LOOKUP_EXPR} AS zone, "
        "    (SELECT h.target_name FROM events h WHERE h.event_type='hail' "
        "       AND h.character_id=ev.character_id AND h.ts<=ev.ts ORDER BY h.ts DESC LIMIT 1) AS npc "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        f"  WHERE ev.event_type='quest' {inner_where}"
        ") t "
        f"{outer_where} ORDER BY ts DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, inner_params + outer_params)
            return cur.fetchall()
    finally:
        conn.close()


def _correlate_task_updates(game, character=""):
    # task_updated (see parsers/eq.py) fires with no detail of its own, but
    # confirmed real (eqlog_Cheerfulish_povar.txt, checked line-by-line):
    # every task_updated shares its exact (second-resolution) ts with either
    # a "You have slain <mob>!" (event_type death, source_name "You") or a
    # "--You have looted ...--" (event_type loot) line for that same
    # character -- never both being wrong, but sometimes more than one
    # candidate ties on ts (a burst of kills/loots in the same second), so
    # line_no proximity breaks the tie. Deliberately NOT correlating by
    # "nearest in time" across seconds -- confirmed real that some
    # task_updated lines (assignment-adjacent, or other non-kill/loot
    # triggers like hailing an NPC) have no same-second kill/loot at all, and
    # guessing a distant one would be worse than leaving it unmatched.
    inner_clauses, inner_params = ["g.code = %s"], [game]
    if character:
        inner_clauses.append("c.name = %s"); inner_params.append(character)
    inner_where = f"AND {' AND '.join(inner_clauses)}"

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.target_name AS task, ev.ts, ev.line_no "
                "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
                f"WHERE ev.event_type='task_updated' {inner_where}",
                inner_params,
            )
            updates = list(cur.fetchall())

            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.line_no, ev.event_type, ev.target_name, ev.amount "
                "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
                f"WHERE (ev.event_type='loot' OR (ev.event_type='death' AND ev.source_name='You')) {inner_where}",
                inner_params,
            )
            triggers = cur.fetchall()
    finally:
        conn.close()

    triggers_by_key = {}
    for t in triggers:
        triggers_by_key.setdefault((t["character_id"], t["ts"]), []).append(t)

    for u in updates:
        candidates = triggers_by_key.get((u["character_id"], u["ts"]), [])
        if not candidates:
            u["trigger"] = None
            continue
        best = min(candidates, key=lambda t: abs(t["line_no"] - u["line_no"]))
        if best["event_type"] == "death":
            u["trigger"] = f'slain {best["target_name"]}'
        else:
            qty = best["amount"] or 1
            u["trigger"] = f'looted {best["target_name"]}' if qty == 1 else f'looted {qty} {best["target_name"]}'
    return updates


def _compute_tasks(game, character="", task="", npc="", zone=""):
    # Live EQ's structured "Tasks" system (task_assigned/task_reward, see
    # parsers/eq.py) -- distinct from /quests/{game}'s dialogue transcript.
    # Neither the assignment nor the reward line names an NPC or zone, so
    # both are correlated the same way /quests already does: zone via
    # whichever zone_change happened most recently before the assignment,
    # npc via whichever NPC most recently spoke (npc_dialogue, not hail --
    # the NPC's own line is what actually explains/hands out the task)
    # before it. reward_at is the nearest task_reward for that same
    # character+task name at or after the assignment -- known gap: a
    # sub-task's reward can be granted under a different name than the task
    # was assigned under (confirmed real: assigned 'Achievements', reward
    # granted for 'Mastering Achievements'), so that reward won't link back
    # to its assignment here; it's not dropped from the game, just from
    # this correlation.
    inner_clauses, inner_params = ["g.code = %s"], [game]
    if character:
        inner_clauses.append("c.name = %s"); inner_params.append(character)
    inner_where = f"AND {' AND '.join(inner_clauses)}"

    outer_clauses, outer_params = [], []
    if task:
        outer_clauses.append("task LIKE %s"); outer_params.append(f"%{task}%")
    if npc:
        outer_clauses.append("npc LIKE %s"); outer_params.append(f"%{npc}%")
    if zone:
        outer_clauses.append("zone LIKE %s"); outer_params.append(f"%{zone}%")
    outer_where = f"WHERE {' AND '.join(outer_clauses)}" if outer_clauses else ""

    sql = (
        "SELECT * FROM ("
        "  SELECT ev.character_id AS character_id, ev.ts AS assigned_at, c.name AS character_name, "
        f"    ev.target_name AS task, {_ZONE_LOOKUP_EXPR} AS zone, "
        "    (SELECT nd.source_name FROM events nd WHERE nd.event_type='npc_dialogue' "
        "       AND nd.character_id=ev.character_id AND nd.ts<=ev.ts ORDER BY nd.ts DESC LIMIT 1) AS npc, "
        "    (SELECT r.ts FROM events r WHERE r.event_type='task_reward' "
        "       AND r.character_id=ev.character_id AND r.target_name=ev.target_name "
        "       AND r.ts>=ev.ts ORDER BY r.ts ASC LIMIT 1) AS reward_at "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        f"  WHERE ev.event_type='task_assigned' {inner_where}"
        ") t "
        f"{outer_where} ORDER BY assigned_at DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, inner_params + outer_params)
            rows = list(cur.fetchall())

            # Window boundaries for attaching updates to the right assignment
            # (a task name can be assigned more than once over a character's
            # lifetime -- dailies, or a sub-task reused across an arc) come
            # from EVERY assignment of that character+task, not just the
            # ones surviving the outer task/npc/zone filter above, or a
            # narrow npc/zone search could cut a window short and misattach
            # an update to the wrong assignment.
            cur.execute(
                "SELECT ev.character_id, ev.target_name AS task, ev.ts AS assigned_at "
                "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
                f"WHERE ev.event_type='task_assigned' {inner_where}",
                inner_params,
            )
            all_assignments = cur.fetchall()
    finally:
        conn.close()

    windows = {}  # (character_id, task) -> sorted list of assigned_at
    for a in all_assignments:
        windows.setdefault((a["character_id"], a["task"]), []).append(a["assigned_at"])
    for lst in windows.values():
        lst.sort()

    updates = _correlate_task_updates(game, character)
    updates_by_key = {}
    for u in updates:
        updates_by_key.setdefault((u["character_id"], u["task"]), []).append(u)
    for lst in updates_by_key.values():
        lst.sort(key=lambda u: u["ts"])

    for r in rows:
        key = (r["character_id"], r["task"])
        same_task_starts = windows.get(key, [r["assigned_at"]])
        later_starts = [ts for ts in same_task_starts if ts > r["assigned_at"]]
        window_end = min(later_starts) if later_starts else None
        r["updates"] = [
            u for u in updates_by_key.get(key, [])
            if r["assigned_at"] <= u["ts"] and (window_end is None or u["ts"] < window_end)
        ]

    return rows


_CURRENCY_TEXT_RE = re.compile(r"^[\d,]+\s*(platinum|gold|silver|copper)\b", re.IGNORECASE)


_CHATTER_EXCLUDED_NPCS = {"voidling"}  # the raid encounter generator, not a real dialogue NPC


def _is_chatter_excluded_npc(name):
    lower = name.lower()
    if lower in _CHATTER_EXCLUDED_NPCS:
        return True
    # Translocators just move you between zones (see _compute_zone_translocators,
    # which surfaces that as its own zone-connection method) -- hailing one
    # isn't real quest dialogue.
    return lower.startswith("translocator ")


def _is_test_dummy(name):
    # Overseer Rael's sparring-partner NPCs (confirmed real, 2026-08-24:
    # Rael's own npc_dialogue lists 15 valid levels -- "[10], [15], [20],
    # ..., [70]" -- spawned as "Test <spelled-out level>", e.g. "Test
    # Fifty"/"Test Seventy", both confirmed real in the events table).
    # Prefix-matched rather than hardcoding all 15 name variants -- robust
    # to Rael's list changing, and "Test " (with the trailing space) doesn't
    # collide with any real name in the data (e.g. "Testimony of Truth" has
    # no space after "Test", so it's untouched). These have artificial
    # unlimited HP for testing, so they're excluded wherever NPC health/
    # level/rare stats get aggregated -- otherwise they'd skew a zone's
    # level range or show up as meaningless "NPCs" on a Zone Info page.
    return bool(name) and name.startswith("Test ")


def _compute_dialogue(game, character="", npc="", zone="", exclude_chatter=False):
    # Classic EQ quests aren't structured turn-ins like EQ2's -- they're
    # branching NPC dialogue (bracketed [keywords] you echo back) plus
    # whatever you're handed in return. There's no logged "give item to
    # NPC" action at all (confirmed empty across a 425k-line real log), so
    # this can't produce a single "quest completed" row the way EQ2's does;
    # instead it's a flat chronological transcript of hail/say/npc_dialogue/
    # reward events, filterable by npc/zone, so a real dialogue tree emerges
    # from repeated visits over time. zone is correlated the same way
    # /loot already does.
    inner_clauses, inner_params = ["g.code = %s"], [game]
    if character:
        inner_clauses.append("c.name = %s"); inner_params.append(character)
    inner_where = f"AND {' AND '.join(inner_clauses)}"

    outer_clauses, outer_params = [], []
    if npc:
        outer_clauses.append("npc LIKE %s"); outer_params.append(f"%{npc}%")
    if zone:
        outer_clauses.append("zone LIKE %s"); outer_params.append(f"%{zone}%")
    outer_where = f"WHERE {' AND '.join(outer_clauses)}" if outer_clauses else ""

    def branch(event_type, npc_expr, text_expr):
        return (
            "SELECT ev.ts, c.name AS character_name, ev.event_type AS kind, "
            f"{npc_expr} AS npc, {text_expr} AS text, ev.amount AS amount, "
            f"{_ZONE_LOOKUP_EXPR} AS zone "
            "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
            f"WHERE ev.event_type='{event_type}' {inner_where}"
        )

    branches = [
        branch("hail", "ev.target_name", "NULL"),
        branch("say", "NULL", "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.text'))"),
        branch("npc_dialogue", "ev.source_name", "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.text'))"),
        branch("reward", "ev.source_name", "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.text'))"),
        # "You offered 1 Letter For Doug to Doug." -- the actual turn-in
        # action (see h_item_offer); item name lives in extra, qty in amount.
        branch("item_offer", "ev.target_name", "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.item'))"),
        # "You complete the trade with Doug." -- no text of its own, just a
        # marker that an offer succeeded.
        branch("trade_complete", "ev.target_name", "NULL"),
    ]
    sql = "SELECT * FROM (" + " UNION ALL ".join(branches) + ") t " + f"{outer_where} ORDER BY character_name, ts"

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (inner_params * len(branches)) + outer_params)
            # pymysql's fetchall() returns () for zero rows but a list for
            # one-or-more -- list() here so the `rows + reward_extras` below
            # doesn't blow up on any NPC with no dialogue lines at all (i.e.
            # most plain combat mobs).
            rows = list(cur.fetchall())

            # The reward for a trade isn't always the currency-from-npc
            # `reward` event -- confirmed real: the one trade in the log
            # (Doug/Letter For Doug) paid out as a plain XP gain instead,
            # with no `reward` event at all. Rewards can also be a faction
            # adjustment, or several of these at once (XP + faction + a
            # `reward` row, all for the same trade). None of those carry an
            # npc of their own, so they're fetched separately (scoped only
            # by character/game, not npc/zone -- those filters apply below,
            # after attaching them to whichever trade_complete they belong
            # to) and matched to the nearest trade_complete within
            # TRADE_REWARD_WINDOW_S for the same character.
            exp_rows = faction_rows = []
            if any(r["kind"] == "trade_complete" for r in rows):
                cur.execute(
                    "SELECT c.name AS character_name, ev.ts, JSON_EXTRACT(ev.extra,'$.percent') AS percent "
                    "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
                    f"WHERE ev.event_type='exp' {inner_where}",
                    inner_params,
                )
                exp_rows = cur.fetchall()
                cur.execute(
                    "SELECT c.name AS character_name, ev.ts, ev.target_name AS faction, ev.amount AS delta "
                    "FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
                    f"WHERE ev.event_type='faction' {inner_where}",
                    inner_params,
                )
                faction_rows = cur.fetchall()
    finally:
        conn.close()

    TRADE_REWARD_WINDOW_S = 10  # the one confirmed real trade paid its XP
    # in the same second as "You complete the trade..."; generous margin
    # against timing jitter without reaching into unrelated combat XP/faction.

    reward_extras = []
    for tc in (r for r in rows if r["kind"] == "trade_complete"):
        for er in exp_rows:
            if er["character_name"] == tc["character_name"] and abs((er["ts"] - tc["ts"]).total_seconds()) <= TRADE_REWARD_WINDOW_S:
                reward_extras.append({
                    "ts": er["ts"], "character_name": tc["character_name"], "kind": "exp",
                    "npc": tc["npc"], "zone": tc["zone"],
                    "text": f'{float(er["percent"]):.3g}% experience', "amount": None,
                })
        for fr in faction_rows:
            if fr["character_name"] == tc["character_name"] and abs((fr["ts"] - tc["ts"]).total_seconds()) <= TRADE_REWARD_WINDOW_S:
                sign = "+" if fr["delta"] >= 0 else ""
                reward_extras.append({
                    "ts": fr["ts"], "character_name": tc["character_name"], "kind": "faction",
                    "npc": tc["npc"], "zone": tc["zone"],
                    "text": f'{fr["faction"]} {sign}{fr["delta"]}', "amount": None,
                })
    rows = sorted(rows + reward_extras, key=lambda r: (r["character_name"], r["ts"]))

    # Collapse repeats of the exact same line (everything but ts) -- e.g. a
    # generic mob's canned bark ("A grikbar kobold says, 'Grrrrr...'")
    # showing up dozens of times is noise, not a new dialogue-tree branch.
    # Rows are already ordered by character_name, ts, so the first time a
    # key is seen is its earliest occurrence; keep that one.
    seen = set()
    deduped = []
    for r in rows:
        # npc lowered in the key -- same sentence-position casing variance
        # as elsewhere (a no-article NPC's name can be capitalized or not
        # depending on where in the sentence it landed), so two otherwise-
        # identical lines shouldn't dodge dedup just because of that.
        key = (r["character_name"], r["kind"], (r["npc"] or "").lower(), r["zone"], r["text"], r["amount"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    rows = deduped

    if exclude_chatter:
        # /quests wants to hide pure ambient chatter (a generic mob's bark
        # with no [bracketed] keyword and no reward attached) -- that data
        # isn't lost, it's just not quest-relevant enough for the aggregate
        # transcript; it's still on that NPC's own /npcs page (which calls
        # _compute_dialogue without this flag). Qualification is per-NPC,
        # not per-line: once an NPC has shown even one bracketed line or a
        # reward, ALL of that NPC's rows here are kept (including the hail/
        # say around it) so the surrounding conversation still reads in
        # context, rather than showing isolated bracketed lines with no
        # setup. "say" rows have no npc attribution in this data model (the
        # log doesn't say who you were replying to) and so never qualify on
        # their own, same as npc_detail's existing npc-filtered view.
        # Grouped case-insensitively -- e.g. a no-article NPC's reward line
        # (npc from a mid-sentence target, naturally lowercase) and its own
        # dialogue line (npc from source_name, always sentence-initial so
        # always capitalized) are the same real NPC but different raw
        # casing; without this, one could qualify the NPC into quest_npcs
        # while the other silently fails the membership check below.
        quest_npcs = {
            r["npc"].lower() for r in rows
            if r["npc"] and (
                r["kind"] in ("reward", "item_offer", "trade_complete")
                or (r["kind"] == "npc_dialogue" and r["text"] and "[" in r["text"])
            )
            and not _is_chatter_excluded_npc(r["npc"])
        }
        rows = [r for r in rows if r["npc"] and r["npc"].lower() in quest_npcs]

    # A reward whose text doesn't read as a currency amount is (once bare
    # item-reward parsing exists -- see h_npc_reward's known gaps) an item
    # name, worth linking to the loot report so you can see every mob/zone
    # that item drops from elsewhere, rather than tracking that separately.
    for r in rows:
        r["loot_link"] = None
        if r["kind"] == "reward" and r["text"] and not _CURRENCY_TEXT_RE.match(r["text"]):
            r["loot_link"] = f"/loot/{game}?item={quote(r['text'])}"
    return rows


QUESTS_RECENT_LIMIT = 20  # anything deeper than this is what npc/character/zone
# search is for -- keeps this page a quick recent-activity glance rather than
# an ever-growing full history.


@app.get("/quests/eql", response_class=HTMLResponse)
def quests_report_eql(request: Request, character: str = "", npc: str = "", zone: str = ""):
    rows = _compute_dialogue("eql", character, npc, zone, exclude_chatter=True)
    truncated = len(rows) > QUESTS_RECENT_LIMIT
    rows = sorted(rows, key=lambda r: r["ts"])[-QUESTS_RECENT_LIMIT:]
    return templates.TemplateResponse(request, "dialogue.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
        "unresolved_zone_starts": _unresolved_zone_starts("eql"),
        "filters": {"character": character, "npc": npc, "zone": zone},
        "truncated": truncated, "limit": QUESTS_RECENT_LIMIT,
    })


@app.get("/quests/eq", response_class=HTMLResponse)
def quests_report_eq(request: Request, character: str = "", npc: str = "", zone: str = ""):
    rows = _compute_dialogue("eq", character, npc, zone, exclude_chatter=True)
    truncated = len(rows) > QUESTS_RECENT_LIMIT
    rows = sorted(rows, key=lambda r: r["ts"])[-QUESTS_RECENT_LIMIT:]
    return templates.TemplateResponse(request, "dialogue.html", {
        "rows": rows, "game": "eq", "game_label": "EverQuest",
        "unresolved_zone_starts": _unresolved_zone_starts("eq"),
        "filters": {"character": character, "npc": npc, "zone": zone},
        "truncated": truncated, "limit": QUESTS_RECENT_LIMIT,
    })


@app.get("/tasks/eq", response_class=HTMLResponse)
def tasks_report_eq(request: Request, character: str = "", task: str = "", npc: str = "", zone: str = ""):
    rows = _compute_tasks("eq", character, task, npc, zone)
    return templates.TemplateResponse(request, "tasks.html", {
        "rows": rows, "game": "eq", "game_label": "EverQuest",
        "unresolved_zone_starts": _unresolved_zone_starts("eq"),
        "filters": {"character": character, "task": task, "npc": npc, "zone": zone},
    })


def _compute_zones(game, character="", zone="", start_ts="", end_ts=""):
    # This is the expensive one: the LEFT JOIN below re-scans events across
    # every zone-visit session's full time window to compute combat stats --
    # fine for a handful of sessions, slow across a character's entire
    # history (this is what was taking a while to load with no filters at
    # all). So, same as /loot and /npcs, this only ever runs against
    # whatever character/zone/date-range the user actually asks for -- see
    # the routes below, which don't call this at all with no filters given.
    char_clauses, char_params = ["g.code = %s"], [game]
    if character:
        char_clauses.append("c.name = %s"); char_params.append(character)
    char_where = f"AND {' AND '.join(char_clauses)}"

    # zone/date filters narrow which *resulting sessions* get shown, not
    # which raw zone_change rows compute the LEAD() boundary above -- doing
    # it the other way (filtering the zone_change events themselves to just
    # the searched zone/date) would silently corrupt left_at/duration for
    # every session that isn't the character's very last one, by skipping
    # over the real intervening zone changes the LEAD() needs to see.
    outer_clauses, outer_params = [], []
    if zone:
        outer_clauses.append("zs.zone LIKE %s"); outer_params.append(f"%{zone}%")
    if start_ts:
        outer_clauses.append("zs.entered_at >= %s"); outer_params.append(start_ts)
    if end_ts:
        outer_clauses.append("zs.entered_at <= %s"); outer_params.append(end_ts)
    outer_where = f"AND {' AND '.join(outer_clauses)}" if outer_clauses else ""

    # Zone "sessions" are the gap between one zone_change and the character's
    # next one; combat stats for that session are whatever damage/kill/melee
    # events from you fall inside that time window. A NULL left_at means no
    # further zone_change was ever recorded (see the logout/file-close
    # fallback below).
    sql = (
        "WITH zone_sessions AS ("
        "  SELECT e.character_id, e.target_name AS zone, e.ts AS entered_at, "
        "         LEAD(e.ts) OVER (PARTITION BY e.character_id ORDER BY e.ts) AS left_at "
        "  FROM events e JOIN games g ON e.game_id=g.id JOIN characters c ON e.character_id=c.id "
        f"  WHERE e.event_type = 'zone_change' {char_where}"
        ") "
        "SELECT zs.character_id, c.name AS character_name, g.code AS game, zs.zone, zs.entered_at, zs.left_at, "
        "  MIN(CASE WHEN e.event_type IN ('melee','spell_damage','ability_damage') AND e.source_type='you' THEN e.ts END) AS first_fight_at, "
        "  MAX(CASE WHEN e.event_type IN ('melee','spell_damage','ability_damage') AND e.source_type='you' THEN e.ts END) AS last_fight_at, "
        "  SUM(CASE WHEN e.event_type IN ('melee','spell_damage','ability_damage') AND e.source_type='you' THEN e.amount ELSE 0 END) AS total_damage, "
        # not just kills you personally landed -- group/raid content means
        # someone else often gets the credit, but the mob still died on your watch
        "  SUM(CASE WHEN e.event_type='death' AND e.target_type != 'you' THEN 1 ELSE 0 END) AS kills, "
        "  SUM(CASE WHEN e.event_type='melee' AND e.source_type='you' THEN 1 ELSE 0 END) AS swings, "
        "  SUM(CASE WHEN e.event_type='melee' AND e.source_type='you' AND e.outcome IN ('hit','crit') THEN 1 ELSE 0 END) AS landed, "
        "  SUM(CASE WHEN e.event_type='melee' AND e.source_type='you' AND e.outcome='crit' THEN 1 ELSE 0 END) AS crits "
        "FROM zone_sessions zs "
        "JOIN characters c ON c.id = zs.character_id "
        "JOIN games g ON g.id = c.game_id "
        "LEFT JOIN events e ON e.character_id = zs.character_id AND e.ts >= zs.entered_at "
        "  AND (zs.left_at IS NULL OR e.ts < zs.left_at) "
        f"WHERE 1=1 {outer_where} "
        "GROUP BY zs.character_id, zs.zone, zs.entered_at, zs.left_at "
        "ORDER BY zs.entered_at DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, char_params + outer_params)
            rows = cur.fetchall()

            # A NULL left_at doesn't necessarily mean "still in that zone" --
            # it could just as well be the log ending (logout, crash, /log
            # off) with no further zone_change ever recorded. There's no
            # explicit logout line to key off of, so the character's last
            # recorded event of any kind is the best available stand-in for
            # "when the log stopped" -- only fetched for characters that
            # actually have an open (NULL left_at) session, not everyone.
            char_ids = {r["character_id"] for r in rows if r["left_at"] is None}
            last_activity = {}
            if char_ids:
                fmt = ",".join(["%s"] * len(char_ids))
                cur.execute(
                    f"SELECT character_id, MAX(ts) AS last_ts FROM events WHERE character_id IN ({fmt}) GROUP BY character_id",
                    list(char_ids),
                )
                last_activity = {r["character_id"]: r["last_ts"] for r in cur.fetchall()}

            # Per-NPC-name kill counts for each session, to validate how many
            # of each mob were actually involved (e.g. after merging "A
            # jeering gargoyle"/"a jeering gargoyle" into one name) rather
            # than just the single combined `kills` total above. Bulk-fetched
            # and bucketed in Python via bisect -- same reasoning as
            # _compute_npc_combat_tiers's docstring: a correlated per-session
            # subquery here would be the same slow anti-pattern.
            all_char_ids = sorted({r["character_id"] for r in rows})
            deaths_by_char = {}
            if all_char_ids:
                fmt = ",".join(["%s"] * len(all_char_ids))
                cur.execute(
                    f"SELECT character_id, ts, target_name FROM events "
                    f"WHERE character_id IN ({fmt}) AND event_type='death' AND target_type != 'you' "
                    "ORDER BY character_id, ts",
                    all_char_ids,
                )
                for d in cur.fetchall():
                    deaths_by_char.setdefault(d["character_id"], {"ts": [], "name": []})
                    deaths_by_char[d["character_id"]]["ts"].append(d["ts"])
                    deaths_by_char[d["character_id"]]["name"].append(d["target_name"])
    finally:
        conn.close()

    for r in rows:
        total_damage = float(r["total_damage"] or 0)
        r["total_damage"] = total_damage
        r["left_at_estimated"] = False
        if r["left_at"] is None:
            fallback = last_activity.get(r["character_id"])
            if fallback and fallback > r["entered_at"]:
                r["left_at"] = fallback
                r["left_at_estimated"] = True
        if r["entered_at"] and r["left_at"]:
            r["duration_min"] = round((r["left_at"] - r["entered_at"]).total_seconds() / 60, 1)
        else:
            r["duration_min"] = None
        if r["first_fight_at"] and r["last_fight_at"]:
            span = (r["last_fight_at"] - r["first_fight_at"]).total_seconds()
            r["dps"] = round(total_damage / span, 1) if span > 0 else total_damage
        else:
            r["dps"] = None
        r["hit_pct"] = round(r["landed"] / r["swings"] * 100, 1) if r["swings"] else None
        r["crit_pct"] = round(r["crits"] / r["landed"] * 100, 1) if r["landed"] else None

        d = deaths_by_char.get(r["character_id"], {"ts": [], "name": []})
        lo = bisect.bisect_left(d["ts"], r["entered_at"])
        hi = bisect.bisect_right(d["ts"], r["left_at"]) if r["left_at"] else len(d["ts"])
        counts = {}
        for name in d["name"][lo:hi]:
            counts[name] = counts.get(name, 0) + 1
        r["npc_kills"] = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    return rows


@app.get("/zones/eql", response_class=HTMLResponse)
def zones_report_eql(request: Request, character: str = "", zone: str = "", start_ts: str = "", end_ts: str = ""):
    searched = bool(character or zone or start_ts or end_ts)
    rows = _compute_zones("eql", character, zone, start_ts, end_ts) if searched else []
    return templates.TemplateResponse(request, "zones.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
        "filters": {"character": character, "zone": zone, "start_ts": start_ts, "end_ts": end_ts},
        "searched": searched,
    })


@app.get("/zones/eq", response_class=HTMLResponse)
def zones_report_eq(request: Request, character: str = "", zone: str = "", start_ts: str = "", end_ts: str = ""):
    searched = bool(character or zone or start_ts or end_ts)
    rows = _compute_zones("eq", character, zone, start_ts, end_ts) if searched else []
    return templates.TemplateResponse(request, "zones.html", {
        "rows": rows, "game": "eq", "game_label": "EverQuest",
        "filters": {"character": character, "zone": zone, "start_ts": start_ts, "end_ts": end_ts},
        "searched": searched,
    })


GATE_SPELLS = ("Gate", "Origin")
GATE_EXCLUSION_WINDOW_S = 60  # see plan notes: real gate/origin casts land the
# resulting zone_change 9-41s later in the actual log; anything beyond that
# observed in the data is clearly an unrelated later zone change, not caused
# by that cast.

# Classic EQ's "Plane of Knowledge Book" -- a static, no-cast zone item found
# in nearly every zone that teleports directly to/from PoK. Only confirmed
# for live EQ so far (not EQL/EQ2, hence keyed by game rather than a bare
# constant); a PoK-involved transition with no spell_cast in the preceding
# GATE_EXCLUSION_WINDOW_S is assumed to be a book trip, per the user's own
# framing -- there's no distinguishing log text for "clicked a book" beyond
# that absence.
POK_ZONE_BY_GAME = {"eq": "The Plane of Knowledge"}


def _base_zone_expr(alias="ev"):
    # Every zone_change event already carries base_zone/tier/solo in its own
    # `extra` (see parse_zone_tier() in eq_legends.py) -- this correlates any
    # OTHER event (con, npc_dialogue, loot, ...) to whichever zone_change
    # happened most recently before it, same "most recent prior row"
    # technique as _ZONE_LOOKUP_EXPR, but resolving to the base zone name
    # (e.g. "Permafrost Keep") rather than the raw tiered string (e.g.
    # "Permafrost Keep 4 (Refined)") so instance variants of the same zone
    # aggregate together on the Zone Info pages.
    return (
        "(SELECT JSON_UNQUOTE(JSON_EXTRACT(z.extra,'$.base_zone')) FROM events z "
        f"WHERE z.event_type='zone_change' AND z.character_id={alias}.character_id "
        f"AND z.ts<={alias}.ts ORDER BY z.ts DESC LIMIT 1)"
    )


def _tier_solo_exprs(alias="ev"):
    # tier/solo from the same "most recent zone_change" correlation as
    # _base_zone_expr, kept as independent correlated subqueries rather than
    # one combined lookup for simplicity -- in practice a character's
    # zone_change rows are never close enough in time for the
    # ORDER BY ts DESC LIMIT 1 tie-break to matter.
    tier = (
        "(SELECT JSON_EXTRACT(z.extra,'$.tier') FROM events z "
        f"WHERE z.event_type='zone_change' AND z.character_id={alias}.character_id "
        f"AND z.ts<={alias}.ts ORDER BY z.ts DESC LIMIT 1)"
    )
    solo = (
        "(SELECT JSON_EXTRACT(z.extra,'$.solo') FROM events z "
        f"WHERE z.event_type='zone_change' AND z.character_id={alias}.character_id "
        f"AND z.ts<={alias}.ts ORDER BY z.ts DESC LIMIT 1)"
    )
    return tier, solo


def _compute_zone_list(game):
    # base_zone is resolved via a single bulk zone_change fetch + Python
    # bisect (per character), not a correlated SQL subquery per row
    # (_base_zone_expr()) -- confirmed real (2026-08-24): with ~2.7M eql
    # events, the _base_zone_expr() version of this function took ~48s
    # (37s of that in the npc_by_zone query alone, measured via
    # SHOW PROCESSLIST). Same root cause and fix as
    # [[feedback-correlated-subquery-per-row-not-per-group]], already
    # applied to /loot -- see that function's comment for the pattern this
    # mirrors.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.ts, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.base_zone')) AS zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='zone_change' AND g.code=%s ORDER BY ev.character_id, ev.ts",
                (game,),
            )
            zone_changes = cur.fetchall()

            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.source_name AS npc, ev.amount AS level, "
                "  JSON_EXTRACT(ev.extra,'$.rare')=true AS rare "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='con' AND g.code=%s",
                (game,),
            )
            con_rows_raw = cur.fetchall()

            # Level range is restricted to NPCs actually fought (not just
            # /con'd) -- a zone's quest-giver/vendor/lore NPCs are often
            # much higher level than anything you'd actually fight there,
            # and including them skews the range upward into something
            # misleading. "Fought" = appeared as either side of a combat
            # event; rare_count below deliberately stays unrestricted (a
            # rare you've conned but not yet fought is still worth knowing
            # about).
            cur.execute(
                "SELECT DISTINCT name FROM ("
                "  SELECT e.source_name AS name FROM events e JOIN games g ON e.game_id=g.id "
                "    WHERE g.code=%s AND e.event_type IN "
                "      ('melee','spell_damage','spell_effect','spell_resist','death') "
                "  UNION "
                "  SELECT e.target_name AS name FROM events e JOIN games g ON e.game_id=g.id "
                "    WHERE g.code=%s AND e.event_type IN "
                "      ('melee','spell_damage','spell_effect','spell_resist','death')"
                ") t WHERE name IS NOT NULL",
                (game, game),
            )
            # Matched case-insensitively: the same mob's name can show up
            # both capitalized ("Orc centurion") and lowercase ("orc
            # centurion") depending on whether the client happened to
            # capitalize it as the first word of a sentence -- same root
            # cause as the leading-article "A"/"An" normalization in
            # normalize_actor_name(), just without an article to anchor on,
            # so it isn't caught there. Confirmed real in an eq log
            # (Emperor Crush's "Orc centurion" appears both ways). rare_names
            # is folded the same way so the same rare mob doesn't inflate
            # rare_count by counting as two.
            fought_names = {r["name"].lower() for r in cur.fetchall() if r["name"]}

            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.source_name "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type IN ('con','npc_dialogue') AND g.code=%s",
                (game,),
            )
            npc_rows_raw = cur.fetchall()

            cur.execute("SELECT zone, level_min_override, level_max_override, note FROM zone_info")
            overrides = {r["zone"]: r for r in cur.fetchall()}
    finally:
        conn.close()

    # Same "most recent zone_change at or before ts, per character" lookup
    # as _base_zone_expr, done once here instead of as a correlated SQL
    # subquery -- see this function's comment above for why.
    zc_by_char: dict = {}
    for r in zone_changes:
        zc = zc_by_char.setdefault(r["character_id"], {"ts": [], "zone": []})
        zc["ts"].append(r["ts"])
        zc["zone"].append(r["zone"])

    def zone_at(character_id, ts):
        zc = zc_by_char.get(character_id)
        if not zc:
            return None
        i = bisect.bisect_right(zc["ts"], ts) - 1
        return zc["zone"][i] if i >= 0 else None

    zones = sorted({z for z in (r["zone"] for r in zone_changes) if z})

    con_by_zone = {}
    for r in con_rows_raw:
        if _is_test_dummy(r["npc"]):
            continue
        zone = zone_at(r["character_id"], r["ts"])
        if not zone:
            continue
        entry = con_by_zone.setdefault(zone, {"levels": [], "rare_names": set()})
        if r["rare"]:
            entry["rare_names"].add(r["npc"].lower())
        if r["npc"].lower() in fought_names:
            entry["levels"].append(r["level"])

    zone_npc_sets: dict = {}
    for r in npc_rows_raw:
        if _is_test_dummy(r["source_name"]):
            continue
        zone = zone_at(r["character_id"], r["ts"])
        if not zone:
            continue
        zone_npc_sets.setdefault(zone, set()).add(r["source_name"])
    npc_by_zone = {zone: len(names) for zone, names in zone_npc_sets.items()}

    rows = []
    for zone in zones:
        con_stats = con_by_zone.get(zone, {"levels": [], "rare_names": set()})
        override = overrides.get(zone)
        levels = con_stats["levels"]
        level_min = min(levels) if levels else None
        level_max = max(levels) if levels else None
        if override:
            if override["level_min_override"] is not None:
                level_min = override["level_min_override"]
            if override["level_max_override"] is not None:
                level_max = override["level_max_override"]
        rows.append({
            "zone": zone,
            "level_min": level_min,
            "level_max": level_max,
            "npc_count": npc_by_zone.get(zone, 0),
            "rare_count": len(con_stats["rare_names"]),
            "note": override["note"] if override else None,
        })
    return rows


def _translocator_routes(game):
    """Raw (translocator, character_id, from_zone, to_zone, hail_ts,
    zone_ts) rows: a hail directed at an NPC named "Translocator ..."
    followed by a zone_change within GATE_EXCLUSION_WINDOW_S for that same
    character -- confirmed real (12-19s gaps for every real translocator
    hail in the log, well within that window). Translocators offer a menu
    of destinations in their own dialogue text ("[travel to Ocean of
    Tears]", "[Qeynos]", ...), but that flavor text doesn't reliably match
    the canonical zone names used everywhere else here (e.g. "Qeynos" isn't
    itself a real zone -- "North Qeynos"/"South Qeynos" are), so this uses
    the same real zone_change correlation as everything else instead of
    parsing the dialogue, same as _compute_zone_connections's gate-cast
    exclusion. Shared by _compute_zone_translocators (for display) and
    _compute_zone_connections (to exclude these from the walkable-
    connection graph -- a translocator ride isn't a walkable path either).
    """
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.target_name AS translocator, "
                f"{_base_zone_expr()} AS from_zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='hail' AND g.code=%s AND ev.target_name LIKE %s",
                (game, "Translocator %"),
            )
            hails = cur.fetchall()

            cur.execute(
                "SELECT ev.character_id, ev.ts, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.base_zone')) AS zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='zone_change' AND g.code=%s",
                (game,),
            )
            zone_changes = cur.fetchall()
    finally:
        conn.close()

    zc_by_char = {}
    for zc in zone_changes:
        zc_by_char.setdefault(zc["character_id"], []).append(zc)
    for lst in zc_by_char.values():
        lst.sort(key=lambda r: r["ts"])

    routes = []
    for h in hails:
        for zc in zc_by_char.get(h["character_id"], []):
            gap = (zc["ts"] - h["ts"]).total_seconds()
            if gap < 0:
                continue
            if gap <= GATE_EXCLUSION_WINDOW_S:
                routes.append({
                    "translocator": h["translocator"], "character_id": h["character_id"],
                    "from_zone": h["from_zone"], "to_zone": zc["zone"],
                    "hail_ts": h["ts"], "zone_ts": zc["ts"],
                })
            break  # only the next zone_change after this hail matters
    return routes


def _compute_zone_translocators(game):
    """(translocator, from_zone, to_zone, times, last_seen) -- translocators
    seen standing in from_zone and where hailing them actually sent you,
    aggregated across every confirmed real trip. See _translocator_routes."""
    pairs = {}
    for r in _translocator_routes(game):
        key = (r["translocator"], r["from_zone"], r["to_zone"])
        entry = pairs.setdefault(key, {
            "translocator": r["translocator"], "from_zone": r["from_zone"], "to_zone": r["to_zone"],
            "times": 0, "last_seen": r["hail_ts"],
        })
        entry["times"] += 1
        entry["last_seen"] = max(entry["last_seen"], r["hail_ts"])
    return sorted(pairs.values(), key=lambda r: (r["from_zone"] or "", r["translocator"]))


def _zone_transitions(game):
    """(character_id, ts, zone, prev_zone) for every consecutive zone_change
    pair per character where the zone actually changed. Shared by
    _compute_zone_connections and _pok_book_routes -- both need "what
    zone did this transition go from/to", just with different exclusion
    rules layered on top."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "WITH zone_seq AS ("
                "  SELECT e.character_id, e.ts, "
                "    JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.base_zone')) AS zone, "
                "    LAG(JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.base_zone'))) OVER (PARTITION BY e.character_id ORDER BY e.ts) AS prev_zone "
                "  FROM events e JOIN games g ON e.game_id=g.id "
                "  WHERE e.event_type='zone_change' AND g.code=%s"
                ") "
                "SELECT character_id, ts, zone, prev_zone FROM zone_seq "
                "WHERE prev_zone IS NOT NULL AND prev_zone <> zone",
                (game,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def _pok_book_routes(game):
    """Raw (character_id, ts, from_zone, to_zone) transitions classified as a
    PoK Book trip: either side of the transition is POK_ZONE_BY_GAME[game]
    and this character cast no spell at all in the GATE_EXCLUSION_WINDOW_S
    before it (a real Gate/Origin/translocator trip would show a cast or a
    translocator hail -- see _compute_zone_connections -- so absence of any
    cast is what's left to mean "clicked a book"). Empty for any game not in
    POK_ZONE_BY_GAME."""
    pok_zone = POK_ZONE_BY_GAME.get(game)
    if not pok_zone:
        return []

    transitions = [
        tr for tr in _zone_transitions(game)
        if tr["zone"] == pok_zone or tr["prev_zone"] == pok_zone
    ]
    if not transitions:
        return []

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.ts FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='spell_cast' AND ev.source_type='you' AND g.code=%s",
                (game,),
            )
            casts = cur.fetchall()
    finally:
        conn.close()

    casts_by_char = {}
    for c in casts:
        casts_by_char.setdefault(c["character_id"], []).append(c["ts"])

    routes = []
    for tr in transitions:
        char_casts = casts_by_char.get(tr["character_id"], [])
        cast_nearby = any(
            0 <= (tr["ts"] - cast_ts).total_seconds() <= GATE_EXCLUSION_WINDOW_S
            for cast_ts in char_casts
        )
        if cast_nearby:
            continue
        routes.append({
            "character_id": tr["character_id"], "ts": tr["ts"],
            "from_zone": tr["prev_zone"], "to_zone": tr["zone"],
        })
    return routes


def _compute_pok_books(game):
    """(from_zone, to_zone, times, last_seen) pairs aggregated from
    _pok_book_routes, same shape/role as _compute_zone_translocators."""
    pairs = {}
    for r in _pok_book_routes(game):
        key = (r["from_zone"], r["to_zone"])
        entry = pairs.setdefault(key, {"from_zone": key[0], "to_zone": key[1], "times": 0, "last_seen": r["ts"]})
        entry["times"] += 1
        entry["last_seen"] = max(entry["last_seen"], r["ts"])
    return sorted(pairs.values(), key=lambda r: (r["from_zone"] or "", r["to_zone"] or ""))


def _compute_zone_connections(game):
    """(from_zone, to_zone, times, last_seen) pairs derived from consecutive
    zone_change events per character, excluding any transition where a
    Gate/Origin cast, a translocator hail, or a PoK Book trip from that
    character landed in the GATE_EXCLUSION_WINDOW_S before it -- none of
    those are a walkable connection (translocators and PoK Books each get
    their own section -- see _compute_zone_translocators/_compute_pok_books).
    Both zones are base zones (tiered instance variants collapse together,
    same as _compute_zone_list).

    The gate-exclusion check used to be a correlated `NOT EXISTS` subquery in
    SQL -- correct, but ~100x slower than this in practice: MariaDB doesn't
    materialize the small Gate/Origin-cast CTE it's checked against, so it
    re-scans the *whole* events table's spell_cast rows once per zone
    transition (measured 5.67s here vs 0.06s below). Both the transition
    list and the cast list are tiny (hundreds of rows, not hundreds of
    thousands), so doing the exclusion check as a plain Python loop over
    two already-fetched lists is both correct and dramatically faster.
    """
    transitions = _zone_transitions(game)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.character_id, ev.ts FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='spell_cast' AND ev.source_type='you' AND ev.verb IN (%s,%s) AND g.code=%s",
                GATE_SPELLS + (game,),
            )
            gate_casts = cur.fetchall()
    finally:
        conn.close()

    gate_by_char = {}
    for gc in gate_casts:
        gate_by_char.setdefault(gc["character_id"], []).append(gc["ts"])

    translocated_zone_ts = {(r["character_id"], r["zone_ts"]) for r in _translocator_routes(game)}
    book_zone_ts = {(r["character_id"], r["ts"]) for r in _pok_book_routes(game)}

    pairs = {}
    for tr in transitions:
        if (tr["character_id"], tr["ts"]) in translocated_zone_ts:
            continue
        if (tr["character_id"], tr["ts"]) in book_zone_ts:
            continue
        char_casts = gate_by_char.get(tr["character_id"], [])
        gated = any(
            0 <= (tr["ts"] - cast_ts).total_seconds() <= GATE_EXCLUSION_WINDOW_S
            for cast_ts in char_casts
        )
        if gated:
            continue
        key = (tr["prev_zone"], tr["zone"])
        entry = pairs.setdefault(key, {"from_zone": key[0], "to_zone": key[1], "times": 0, "last_seen": tr["ts"]})
        entry["times"] += 1
        entry["last_seen"] = max(entry["last_seen"], tr["ts"])

    return sorted(pairs.values(), key=lambda r: (r["from_zone"], -r["times"]))


def _compute_zone_detail(game, zone, tier="all", solo="any"):
    tier_expr, solo_expr = _tier_solo_exprs()
    match_clauses, match_params = [f"{_base_zone_expr()} = %s"], [zone]
    if tier != "all":
        match_clauses.append(f"COALESCE({tier_expr}, 0) = %s"); match_params.append(int(tier))
    if solo != "any":
        match_clauses.append(f"COALESCE({solo_expr}, false) = %s"); match_params.append(solo == "1")
    match_where = " AND ".join(match_clauses)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # tiers/solo actually observed for this zone, to build the toggle UI.
            # These are read directly off each zone_change's own extra (no
            # correlation needed -- unlike tier_expr/solo_expr above, which
            # look up the most recent zone_change *before* some other event).
            cur.execute(
                "SELECT DISTINCT JSON_EXTRACT(e.extra,'$.tier') AS tier, "
                "JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.tier_label')) AS tier_label, "
                "JSON_EXTRACT(e.extra,'$.solo') AS solo "
                "FROM events e JOIN games g ON e.game_id=g.id "
                "WHERE e.event_type='zone_change' AND g.code=%s "
                "AND JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.base_zone')) = %s",
                (game, zone),
            )
            variants = cur.fetchall()

            # NPCs seen: con (with level/rare) and npc_dialogue key off
            # source_name (who's conning/talking); death keys off
            # target_name (who died -- source_name for a death event is
            # the killer, e.g. "You", not the NPC roster we want here).
            # "Test <level>" sparring dummies (see _is_test_dummy) are
            # excluded -- artificial unlimited HP, not real zone content.
            cur.execute(
                "WITH zone_npc_events AS ("
                "  SELECT ev.source_name AS npc, ev.event_type, ev.amount, ev.extra "
                "  FROM events ev JOIN games g ON ev.game_id=g.id "
                "  WHERE ev.event_type IN ('con','npc_dialogue') AND ev.source_name IS NOT NULL "
                "    AND ev.source_name NOT LIKE 'Test %%' "
                f"    AND g.code=%s AND {match_where} "
                "  UNION ALL "
                "  SELECT ev.target_name AS npc, ev.event_type, ev.amount, ev.extra "
                "  FROM events ev JOIN games g ON ev.game_id=g.id "
                "  WHERE ev.event_type='death' AND ev.target_type != 'you' AND ev.target_name IS NOT NULL "
                "    AND ev.target_name NOT LIKE 'Test %%' "
                f"    AND g.code=%s AND {match_where}"
                ") "
                "SELECT npc, "
                "MAX(CASE WHEN event_type='con' THEN amount END) AS level, "
                # MAX() strips JSON_EXTRACT's JSON-typed result down to a
                # plain string ("true"/"false"), so it must be compared
                # against the string 'true' here, not the bare keyword true
                # (which MariaDB happily accepts directly against a raw
                # JSON_EXTRACT() call, but silently mis-compares as 0 once
                # MAX() has touched it -- verified against real data).
                "MAX(CASE WHEN event_type='con' THEN JSON_EXTRACT(extra,'$.rare') END)='true' AS rare, "
                "SUM(CASE WHEN event_type='death' THEN 1 ELSE 0 END) AS kills, "
                "SUM(CASE WHEN event_type='npc_dialogue' THEN 1 ELSE 0 END) AS dialogue_lines "
                "FROM zone_npc_events GROUP BY npc ORDER BY npc",
                [game] + match_params + [game] + match_params,
            )
            npcs = cur.fetchall()

            # loot obtained in this zone (same shape as _compute_loot, scoped to zone)
            cur.execute(
                "SELECT ev.source_name AS npc, ev.target_name AS item, "
                "ROUND(AVG(ev.amount), 1) AS avg_qty, COUNT(*) AS drops "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE ev.event_type='loot' AND g.code=%s AND {match_where} "
                "GROUP BY ev.source_name, ev.target_name ORDER BY npc, item",
                [game] + match_params,
            )
            loot = cur.fetchall()

            # ground spawns picked up here -- no source NPC (unlike loot
            # above), just an item and how many times it's been picked up.
            cur.execute(
                "SELECT ev.target_name AS item, COUNT(*) AS times "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE ev.event_type='ground_spawn' AND g.code=%s AND {match_where} "
                "GROUP BY ev.target_name ORDER BY item",
                [game] + match_params,
            )
            ground_spawns = cur.fetchall()

            # quest/reward items (non-currency reward text) given by NPCs here
            cur.execute(
                "SELECT ev.source_name AS npc, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.text')) AS item, ev.ts "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE ev.event_type='reward' AND g.code=%s AND {match_where} "
                "ORDER BY ev.ts DESC",
                [game] + match_params,
            )
            reward_rows = cur.fetchall()
            quest_items = [r for r in reward_rows if r["item"] and not _CURRENCY_TEXT_RE.match(r["item"])]

            # Completed item turn-ins (item_offer -> trade_complete pairs) --
            # a different signal from the reward-based quest_items above.
            # trade_complete carries no info about what was given, only that
            # an offer succeeded, so items are paired back to whichever
            # item_offer row(s) immediately preceded it for the same NPC
            # (same pairing shape as _compute_dialogue). Confirmed real
            # (Plane of Sky, 2026-08-13): dozens of completed Wind Rune/
            # artifact exchanges with no `reward` event at all -- the
            # reward-only quest_items query above silently missed every one
            # of these, which is what a user noticed as "completed more
            # quests than the loot list accounts for".
            cur.execute(
                "SELECT ev.event_type AS kind, ev.ts, ev.target_name AS npc, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.item')) AS item "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE ev.event_type IN ('item_offer','trade_complete') AND g.code=%s AND {match_where} "
                "ORDER BY ev.ts",
                [game] + match_params,
            )
            trade_events = cur.fetchall()
            pending_offers = {}
            completed_trades = []
            for r in trade_events:
                if r["kind"] == "item_offer":
                    pending_offers.setdefault(r["npc"], []).append(r["item"])
                else:
                    items = pending_offers.pop(r["npc"], [])
                    if items:
                        # Not "items" -- Jinja's dot-lookup on a dict tries
                        # getattr() before __getitem__, and dict already has
                        # a real .items() method, so `t.items` in the
                        # template silently resolves to that bound method
                        # instead of this key (confirmed real: 500 error,
                        # "'builtin_function_or_method' object is not
                        # iterable" from the |join filter).
                        completed_trades.append({"ts": r["ts"], "npc": r["npc"], "items_given": items})
            completed_trades.sort(key=lambda t: t["ts"], reverse=True)

            # rare mobs seen here, cross-referenced with their own drops
            # (from the loot rows already fetched above) per the user's
            # explicit ask to be able to check a rare's drops at a glance.
            # Matched case-insensitively -- same sentence-position
            # capitalization issue as _compute_zone_list's fought_names
            # (e.g. "Orc centurion" vs "orc centurion"), so npcs/loot rows
            # for the same real mob don't fail to cross-reference just
            # because they came from lines with different casing.
            rare_names = {n["npc"] for n in npcs if n["rare"]}
            rares = []
            for name in sorted(rare_names):
                rares.append({
                    "npc": name,
                    "level": next((n["level"] for n in npcs if n["npc"].lower() == name.lower()), None),
                    "drops": [l for l in loot if l["npc"].lower() == name.lower()],
                })

            cur.execute(
                "SELECT level_min_override, level_max_override, note FROM zone_info WHERE zone=%s",
                (zone,),
            )
            override = cur.fetchone()

    finally:
        conn.close()

    return {
        "variants": variants,
        "npcs": npcs,
        "loot": loot,
        "ground_spawns": ground_spawns,
        "quest_items": quest_items,
        "completed_trades": completed_trades,
        "rares": rares,
        "override": override,
    }


@app.get("/zoneinfo/eql", response_class=HTMLResponse)
def zoneinfo_list_eql(request: Request):
    rows = _compute_zone_list("eql")
    return templates.TemplateResponse(request, "zoneinfo_list.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
    })


@app.get("/zoneinfo/eq", response_class=HTMLResponse)
def zoneinfo_list_eq(request: Request):
    rows = _compute_zone_list("eq")
    return templates.TemplateResponse(request, "zoneinfo_list.html", {
        "rows": rows, "game": "eq", "game_label": "EverQuest",
    })


def _render_zoneinfo_detail(request, game, game_label, zone, tier, solo):
    detail = _compute_zone_detail(game, zone, tier, solo)
    connections = _compute_zone_connections(game)
    translocators = _compute_zone_translocators(game)
    books = _compute_pok_books(game)
    return templates.TemplateResponse(request, "zoneinfo_detail.html", {
        "game": game, "game_label": game_label, "zone": zone,
        "tier": tier, "solo": solo,
        "in_from": [c for c in connections if c["to_zone"] == zone],
        "out_to": [c for c in connections if c["from_zone"] == zone],
        "translocators_here": [t for t in translocators if t["from_zone"] == zone],
        "books_here": [b for b in books if b["from_zone"] == zone or b["to_zone"] == zone],
        **detail,
    })


@app.get("/zoneinfo/eql/detail", response_class=HTMLResponse)
def zoneinfo_detail_eql(request: Request, zone: str, tier: str = "all", solo: str = "any"):
    return _render_zoneinfo_detail(request, "eql", "EQ Legends", zone, tier, solo)


@app.get("/zoneinfo/eq/detail", response_class=HTMLResponse)
def zoneinfo_detail_eq(request: Request, zone: str, tier: str = "all", solo: str = "any"):
    return _render_zoneinfo_detail(request, "eq", "EverQuest", zone, tier, solo)


@app.post("/zoneinfo/set-level")
def zoneinfo_set_level(
    zone: str = Form(...), level_min: str = Form(""), level_max: str = Form(""),
    note: str = Form(""), game: str = Form("eql"),
):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO zone_info (zone, level_min_override, level_max_override, note) "
                "VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE level_min_override=VALUES(level_min_override), "
                "level_max_override=VALUES(level_max_override), note=VALUES(note)",
                (zone, level_min or None, level_max or None, note or None),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/zoneinfo/{game}/detail?zone={quote(zone)}", status_code=303)


def _suggest_npc_type(name, rare_or_vendor_stats):
    if rare_or_vendor_stats["vendor"]:
        return "vendor"
    if re.match(r"^(a|an|the)\s", name.strip(), re.IGNORECASE):
        return "mob"
    return "npc"


def _compute_npc_list(game, search="", zone=""):
    # "Test <level>" sparring dummies (see _is_test_dummy) are excluded --
    # artificial unlimited HP, not real content worth searching/browsing.
    sql = (
        "WITH npc_events AS ("
        "  SELECT ev.source_name AS npc, ev.event_type, ev.amount, ev.extra, "
        f"    {_base_zone_expr()} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id "
        "  WHERE g.code=%s AND ev.event_type IN ('con','npc_dialogue','vendor_buy','vendor_sell','loot') "
        "    AND ev.source_name IS NOT NULL AND ev.source_name NOT LIKE 'Test %%' "
        "  UNION ALL "
        "  SELECT ev.target_name AS npc, ev.event_type, ev.amount, ev.extra, "
        f"    {_base_zone_expr()} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id "
        "  WHERE g.code=%s AND ev.event_type='death' AND ev.target_name IS NOT NULL AND ev.target_type != 'you' "
        "    AND ev.target_name NOT LIKE 'Test %%'"
        ") "
        "SELECT npc, "
        "  MIN(CASE WHEN event_type='con' THEN amount END) AS level_min, "
        "  MAX(CASE WHEN event_type='con' THEN amount END) AS level_max, "
        "  MAX(CASE WHEN event_type='con' THEN JSON_EXTRACT(extra,'$.rare') END) = 'true' AS rare, "
        "  (SUM(CASE WHEN event_type IN ('vendor_buy','vendor_sell') THEN 1 ELSE 0 END) > 0) AS vendor, "
        "  SUM(CASE WHEN event_type='npc_dialogue' THEN 1 ELSE 0 END) AS dialogue_lines, "
        "  SUM(CASE WHEN event_type='death' THEN 1 ELSE 0 END) AS kills, "
        "  GROUP_CONCAT(DISTINCT zone ORDER BY zone SEPARATOR ', ') AS zones "
        "FROM npc_events GROUP BY npc"
    )
    params = [game, game]
    having_clauses, having_params = [], []
    if search:
        having_clauses.append("npc LIKE %s"); having_params.append(f"%{search}%")
    if zone:
        having_clauses.append("zones LIKE %s"); having_params.append(f"%{zone}%")
    if having_clauses:
        sql += " HAVING " + " AND ".join(having_clauses)
        params += having_params
    sql += " ORDER BY npc"

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            names = [r["npc"] for r in rows]
            overrides = {}
            if names:
                fmt = ",".join(["%s"] * len(names))
                cur.execute(f"SELECT npc, npc_type, note FROM npc_info WHERE npc IN ({fmt})", names)
                # Matched case-insensitively -- npc_info.npc was saved via
                # whatever casing a different query's GROUP BY happened to
                # pick as that NPC's representative row at the time (e.g.
                # from /npcs/{game}/detail, a separate query from this
                # list), which can differ from this query's own pick for
                # the exact same real NPC.
                overrides = {r["npc"].lower(): r for r in cur.fetchall()}
    finally:
        conn.close()

    for r in rows:
        override = overrides.get(r["npc"].lower())
        r["suggested_type"] = _suggest_npc_type(r["npc"], r)
        r["npc_type"] = (override["npc_type"] if override and override["npc_type"] else r["suggested_type"])
        r["note"] = override["note"] if override else None
    return rows


def _compute_npc_combat_tiers(game, name):
    # Estimated HP per zone tier, from your own damage between the previous
    # kill (or zone entry) and each death -- there's no per-mob-instance ID
    # in the log, so this is an estimate, not exact HP: only source_type='you'
    # damage counts (pet/other-player hits don't), so a group kill undercounts,
    # and two simultaneously-up mobs sharing the same name (see tailer.py's
    # CombatTracker docstring for the same limitation) conflate into one
    # window, which is why min/max can spread wide for common trash names.
    # Level range per tier comes from `con` the same way (EQ2 has no `con`
    # parsing yet, so those rows are always level_min/max=None).
    #
    # Computed by bulk-fetching each character's death/zone_change/damage
    # rows and doing the windowing here via bisect, rather than correlating
    # tier/kill-window with per-row scalar subqueries in SQL -- the SQL
    # version of this (nesting a kill-window scalar subquery inside a
    # per-death correlated subquery) took over two minutes on a busy NPC;
    # this is under 0.1s even for a 200-kill NPC. Same lesson as
    # _compute_zone_list's docstring, applied to a per-character-timeline
    # correlation instead of a per-zone one.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.character_id, d.ts AS death_ts "
                "FROM events d JOIN games g ON d.game_id=g.id "
                "WHERE g.code=%s AND d.event_type='death' AND d.target_name=%s "
                "  AND d.source_type='you' AND d.target_type != 'you' "
                "ORDER BY d.character_id, d.ts",
                (game, name),
            )
            deaths = cur.fetchall()

            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.amount AS level "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE g.code=%s AND ev.event_type='con' AND ev.source_name=%s",
                (game, name),
            )
            cons = cur.fetchall()

            char_ids = sorted({r["character_id"] for r in deaths} | {r["character_id"] for r in cons})
            zone_changes, damage = [], []
            if char_ids:
                fmt = ",".join(["%s"] * len(char_ids))
                cur.execute(
                    f"SELECT character_id, ts, JSON_EXTRACT(extra,'$.tier') AS tier, "
                    "JSON_UNQUOTE(JSON_EXTRACT(extra,'$.tier_label')) AS tier_label "
                    f"FROM events WHERE event_type='zone_change' AND character_id IN ({fmt}) ORDER BY character_id, ts",
                    char_ids,
                )
                zone_changes = cur.fetchall()

                cur.execute(
                    "SELECT character_id, ts, amount FROM events "
                    "WHERE event_type IN ('melee','spell_damage','ability_damage') AND source_type='you' "
                    f"AND target_name=%s AND character_id IN ({fmt}) AND amount IS NOT NULL ORDER BY character_id, ts",
                    [name] + char_ids,
                )
                damage = cur.fetchall()
    finally:
        conn.close()

    if not deaths and not cons:
        return []

    zc_by_char = {}
    for r in zone_changes:
        zc = zc_by_char.setdefault(r["character_id"], {"ts": [], "rows": []})
        zc["ts"].append(r["ts"])
        zc["rows"].append(r)

    def tier_at(char_id, ts):
        zc = zc_by_char.get(char_id)
        if not zc:
            return None, None
        i = bisect.bisect_right(zc["ts"], ts) - 1
        return (zc["rows"][i]["tier"], zc["rows"][i]["tier_label"]) if i >= 0 else (None, None)

    dmg_by_char = {}
    for r in damage:
        dmg = dmg_by_char.setdefault(r["character_id"], {"ts": [], "amt": []})
        dmg["ts"].append(r["ts"])
        dmg["amt"].append(r["amount"])

    deaths_by_char = {}
    for r in deaths:
        deaths_by_char.setdefault(r["character_id"], []).append(r["death_ts"])

    by_tier = {}

    def bucket(tier):
        return by_tier.setdefault(tier, {"tier_label": None, "kills": 0, "hp_totals": [], "levels": []})

    for char_id, char_deaths in deaths_by_char.items():
        zc = zc_by_char.get(char_id, {"ts": [], "rows": []})
        dmg = dmg_by_char.get(char_id, {"ts": [], "amt": []})

        for i, death_ts in enumerate(char_deaths):
            tier, tier_label = tier_at(char_id, death_ts)

            # Window start: the later of this character's previous kill of
            # this same NPC name, or their most recent zone entry -- whichever
            # bounds the fight more tightly, so an unrelated earlier kill
            # (or a stale window reaching across a zone change) doesn't get
            # summed in.
            window_start = char_deaths[i - 1] if i > 0 else None
            zi = bisect.bisect_right(zc["ts"], death_ts) - 1
            if zi >= 0 and (window_start is None or zc["ts"][zi] > window_start):
                window_start = zc["ts"][zi]

            lo = bisect.bisect_right(dmg["ts"], window_start) if window_start else 0
            hi = bisect.bisect_right(dmg["ts"], death_ts)
            kill_total = sum(dmg["amt"][lo:hi])
            if kill_total:
                b = bucket(tier)
                b["kills"] += 1
                b["hp_totals"].append(kill_total)
                if tier_label:
                    b["tier_label"] = tier_label

    for r in cons:
        tier, tier_label = tier_at(r["character_id"], r["ts"])
        b = bucket(tier)
        b["levels"].append(r["level"])
        if tier_label:
            b["tier_label"] = tier_label

    out = []
    for tier, b in by_tier.items():
        row = {
            "tier": tier, "tier_label": b["tier_label"], "kills": b["kills"],
            "level_min": min(b["levels"]) if b["levels"] else None,
            "level_max": max(b["levels"]) if b["levels"] else None,
        }
        if b["hp_totals"]:
            row["hp_min"] = min(b["hp_totals"])
            row["hp_avg"] = round(sum(b["hp_totals"]) / len(b["hp_totals"]))
            row["hp_max"] = max(b["hp_totals"])
        else:
            row["hp_min"] = row["hp_avg"] = row["hp_max"] = None
        out.append(row)
    return sorted(out, key=lambda r: (r["tier"] is None, r["tier"]))


def _compute_npc_detail(game, name, tier="all"):
    tier_expr, _ = _tier_solo_exprs()
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT " + _base_zone_expr() + " AS zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE g.code=%s AND ((ev.source_name=%s AND ev.event_type IN ('con','npc_dialogue','loot','vendor_buy','vendor_sell')) "
                "  OR (ev.target_name=%s AND ev.event_type='death' AND ev.target_type != 'you'))",
                (game, name, name),
            )
            zones = sorted(r["zone"] for r in cur.fetchall() if r["zone"])

            # Tiers this NPC's loot has actually been seen in, to build the
            # loot pulldown -- same "collapse instance variants, toggle
            # separately" idea as the zone detail page's tier toggle.
            cur.execute(
                f"SELECT DISTINCT {tier_expr} AS tier, "
                "JSON_UNQUOTE(JSON_EXTRACT((SELECT z.extra FROM events z WHERE z.event_type='zone_change' "
                "  AND z.character_id=ev.character_id AND z.ts<=ev.ts ORDER BY z.ts DESC LIMIT 1),'$.tier_label')) AS tier_label "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE g.code=%s AND ev.event_type='loot' AND ev.source_name=%s",
                (game, name),
            )
            loot_tiers = cur.fetchall()

            loot_clauses, loot_params = ["ev.event_type='loot'", "ev.source_name=%s"], [name]
            if tier != "all":
                loot_clauses.append(f"COALESCE({tier_expr}, 0) = %s"); loot_params.append(int(tier))
            cur.execute(
                "SELECT ev.target_name AS item, ROUND(AVG(ev.amount),1) AS avg_qty, COUNT(*) AS drops "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                f"WHERE g.code=%s AND {' AND '.join(loot_clauses)} "
                "GROUP BY ev.target_name ORDER BY item",
                [game] + loot_params,
            )
            loot = cur.fetchall()

            cur.execute(
                "SELECT MIN(amount) AS level_min, MAX(amount) AS level_max, "
                "MAX(JSON_EXTRACT(extra,'$.rare'))='true' AS rare "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE g.code=%s AND ev.event_type='con' AND ev.source_name=%s",
                (game, name),
            )
            con_stats = cur.fetchone()

            cur.execute(
                "SELECT ev.target_name AS item, ev.amount AS qty, "
                "JSON_UNQUOTE(JSON_EXTRACT(ev.extra,'$.price_text')) AS price "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE g.code=%s AND ev.event_type='vendor_buy' AND ev.source_name=%s "
                "GROUP BY ev.target_name, ev.amount, price ORDER BY item",
                (game, name),
            )
            catalog = cur.fetchall()
            is_vendor = bool(catalog)
            if not is_vendor:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM events ev JOIN games g ON ev.game_id=g.id "
                    "WHERE g.code=%s AND ev.event_type='vendor_sell' AND ev.source_name=%s",
                    (game, name),
                )
                is_vendor = cur.fetchone()["n"] > 0

            cur.execute("SELECT npc_type, note FROM npc_info WHERE npc=%s", (name,))
            override = cur.fetchone()
    finally:
        conn.close()

    dialogue = _compute_dialogue(game, character="", npc=name, zone="")
    # Best-effort "quest begun/ended" markers -- classic EQ rarely logs a
    # turn-in action at all (only one confirmed real example exists across
    # a 425k-line log: "You offered 1 Letter For Doug to Doug." / "You
    # complete the trade with Doug." -- see h_item_offer/h_trade_complete),
    # so this stays a heuristic, not guaranteed quest tracking. "ended"
    # prefers trade_complete over reward where both exist -- a completed
    # trade is a stronger/more direct completion signal than a reward, and
    # is the ONLY signal at all when the actual reward was XP/faction
    # rather than an item/currency `reward` event (confirmed: the one real
    # trade found gave experience, not a reward line).
    contact_rows = [r for r in dialogue if r["kind"] in ("hail", "npc_dialogue", "item_offer")]
    ended_rows = [r for r in dialogue if r["kind"] in ("reward", "trade_complete")]
    quest_begun_at = min((r["ts"] for r in contact_rows), default=None)
    quest_ended_at = max((r["ts"] for r in ended_rows), default=None)

    suggested_type = _suggest_npc_type(name, {"vendor": is_vendor})
    return {
        "name": name,
        "zones": zones,
        "level_min": con_stats["level_min"],
        "level_max": con_stats["level_max"],
        "rare": bool(con_stats["rare"]),
        "combat_tiers": _compute_npc_combat_tiers(game, name),
        "vendor": is_vendor,
        "catalog": catalog,
        "loot": loot,
        "loot_tiers": loot_tiers,
        "tier": tier,
        "dialogue": dialogue,
        "quest_begun_at": quest_begun_at,
        "quest_ended_at": quest_ended_at,
        "npc_type": (override["npc_type"] if override and override["npc_type"] else suggested_type),
        "suggested_type": suggested_type,
        "note": override["note"] if override else None,
    }


@app.get("/npcs/eql", response_class=HTMLResponse)
def npcs_list_eql(request: Request, search: str = "", zone: str = ""):
    rows = _compute_npc_list("eql", search, zone) if (search or zone) else []
    return templates.TemplateResponse(request, "npc_list.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
        "search": search, "zone": zone, "searched": bool(search or zone),
    })


@app.get("/npcs/eq", response_class=HTMLResponse)
def npcs_list_eq(request: Request, search: str = "", zone: str = ""):
    rows = _compute_npc_list("eq", search, zone) if (search or zone) else []
    return templates.TemplateResponse(request, "npc_list.html", {
        "rows": rows, "game": "eq", "game_label": "EverQuest",
        "search": search, "zone": zone, "searched": bool(search or zone),
    })


@app.get("/npcs/eql/detail", response_class=HTMLResponse)
def npc_detail_eql(request: Request, npc: str, tier: str = "all"):
    detail = _compute_npc_detail("eql", npc, tier)
    return templates.TemplateResponse(request, "npc_detail.html", {
        "game": "eql", "game_label": "EQ Legends", **detail,
    })


@app.get("/npcs/eq/detail", response_class=HTMLResponse)
def npc_detail_eq(request: Request, npc: str, tier: str = "all"):
    detail = _compute_npc_detail("eq", npc, tier)
    return templates.TemplateResponse(request, "npc_detail.html", {
        "game": "eq", "game_label": "EverQuest", **detail,
    })


@app.post("/npc-info/set")
def npc_info_set(npc: str = Form(...), npc_type: str = Form(""), note: str = Form(""), game: str = Form("eql")):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO npc_info (npc, npc_type, note) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE npc_type=VALUES(npc_type), note=VALUES(note)",
                (npc, npc_type or None, note or None),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/npcs/{game}/detail?npc={quote(npc)}", status_code=303)


@app.get("/api/events/since/{last_id}")
def events_since(last_id: int, limit: int = 200):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ts, event_type, source_name, source_type, target_name, target_type, verb, amount, outcome, raw_line "
                "FROM events WHERE id > %s ORDER BY id ASC LIMIT %s",
                (last_id, limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return JSONResponse([{**r, "ts": r["ts"].isoformat()} for r in rows])


DAMAGE_EVENT_TYPES_SQL = "('melee','spell_damage','ability_damage')"

# No parser has a "You stop fighting." marker, so encounters are derived
# purely from activity: out of combat is 5 seconds since the last combat
# action, same idea as the DPS meter's own pause timeout
# (COMBAT_TIMEOUT_SECONDS in tailer.py) -- OR immediately, regardless of the
# timer, on your own death, using Escape, or a zone change (hard stops).
EQL_OUT_OF_COMBAT_SECONDS = 5


def _derive_gap_based_encounters(rows, gap_seconds):
    """rows: chronological (character_id, character_name, game, ts,
    is_hard_stop, is_hard_start, marker_name) rows -- regular activity (both
    flags False) extends the encounter and is closed after gap_seconds of no
    further activity; a hard-stop row (your death, Escape, zone change, or an
    "Encounter Stop" /say marker) closes it immediately at its own
    timestamp, without itself starting a new encounter. A hard-start row
    (an "Encounter Start" /say marker) closes whatever's currently open
    (if anything) the same way a hard-stop does, but then immediately opens
    a new encounter starting at its own ts regardless of the gap timer --
    lets dummy/gear-testing pulls of the same mob with no death involved
    still split into separate encounters instead of merging into one.
    marker_name (only ever set on a hard-start row, e.g. "daggers" from
    "Encounter Start daggers") is carried onto the new encounter as its
    "name", for telling several such runs apart in the results table."""
    open_encounters: dict[int, dict] = {}
    finished = []

    for row in rows:
        cid = row["character_id"]
        current = open_encounters.get(cid)

        # A stale encounter naturally times out before *any* row -- hard-stop
        # or activity -- gets to touch it, so a hard-stop arriving long after
        # the last real action can't retroactively balloon its duration out
        # to the hard-stop's own (much later) timestamp.
        if current is not None and (row["ts"] - current["last_ts"]).total_seconds() > gap_seconds:
            current["stop_ts"] = current["last_ts"]
            finished.append(current)
            current = None
            open_encounters[cid] = None

        if row["is_hard_stop"] or row["is_hard_start"]:
            if current is not None:
                current["stop_ts"] = row["ts"]
                finished.append(current)
                open_encounters[cid] = None
                current = None
            if not row["is_hard_start"]:
                continue
            # fall through: a hard-start immediately opens a fresh encounter
            # at this same row's ts, unlike a plain hard-stop.

        if current is None:
            current = {
                "character_id": cid, "character_name": row["character_name"],
                "game": row["game"], "start_ts": row["ts"], "last_ts": row["ts"], "stop_ts": None,
                "name": row["marker_name"] if row["is_hard_start"] else None,
            }
            open_encounters[cid] = current
        else:
            current["last_ts"] = row["ts"]

    now = datetime.now()
    for current in open_encounters.values():
        if current is None:
            continue
        if (now - current["last_ts"]).total_seconds() > gap_seconds:
            current["stop_ts"] = current["last_ts"]  # naturally ended, just nothing came in to trigger the check above
        finished.append(current)
    finished.sort(key=lambda e: e["start_ts"], reverse=True)
    return finished


def _compute_encounters(character="", start_ts="", end_ts="", npc="", limit=30, ascending=False):
    clauses, params = [], []
    if character:
        clauses.append("c.name = %s"); params.append(character)
    if start_ts:
        clauses.append("ev.ts >= %s"); params.append(start_ts)
    if end_ts:
        clauses.append("ev.ts <= %s"); params.append(end_ts)
    where = f"AND {' AND '.join(clauses)}" if clauses else ""

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # Regular activity: extends the encounter, subject to the
            # gap_seconds timeout. Damage either direction, or a kill BY you
            # (an NPC's death doesn't end your combat -- there may be adds).
            cur.execute(
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, "
                "  0 AS is_hard_stop, 0 AS is_hard_start, NULL AS marker_name "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                "WHERE ("
                f"  (ev.event_type IN {DAMAGE_EVENT_TYPES_SQL} AND (ev.source_type='you' OR ev.target_type='you'))"
                "  OR (ev.event_type='death' AND ev.source_type='you')"
                f") {where} "
                "UNION ALL "
                # Hard stops: close the encounter immediately regardless of
                # the timer -- your own death, Escape, a zone change, or an
                # "Encounter Stop" /say marker (see h_encounter_marker).
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, "
                "  1 AS is_hard_stop, 0 AS is_hard_start, NULL AS marker_name "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                "WHERE ("
                "  (ev.event_type='death' AND ev.target_type='you')"
                "  OR ev.event_type='escape'"
                "  OR (ev.event_type='zone_change' AND ev.source_type='you')"
                "  OR (ev.event_type='encounter_marker' AND ev.verb='stop' AND ev.source_type='you')"
                f") {where} "
                "UNION ALL "
                # Hard start: an "Encounter Start[ name]" /say marker -- closes
                # whatever's open (same as a hard stop, above) but then
                # immediately opens a fresh encounter at its own ts, carrying
                # the marker's optional name onto it. See
                # _derive_gap_based_encounters for the actual behavior.
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, "
                "  0 AS is_hard_stop, 1 AS is_hard_start, ev.target_name AS marker_name "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                "WHERE (ev.event_type='encounter_marker' AND ev.verb='start' AND ev.source_type='you') "
                f"{where} "
                "ORDER BY ts",
                params + params + params,
            )
            activity = cur.fetchall()

            # NPC filter: which (character_id, ts) combat events touched a
            # name matching the search, bulk-fetched once and checked with a
            # binary search (bisect) per encounter window below -- same
            # "fetch once, check in Python" reasoning as the zone-lookup
            # helpers elsewhere in this file, and avoids a correlated
            # subquery per encounter. Applied before the sort+limit slice so
            # e.g. "just Vox today" isn't silently truncated by unrelated
            # encounters eating the limit first.
            npc_hits: dict[int, list] = {}
            if npc:
                cur.execute(
                    "SELECT ev.character_id, ev.ts "
                    "FROM events ev JOIN characters c ON ev.character_id=c.id "
                    f"WHERE ev.event_type IN {DAMAGE_EVENT_TYPES_SQL} "
                    "AND (ev.source_name LIKE %s OR ev.target_name LIKE %s) "
                    f"{where} ORDER BY ev.ts",
                    [f"%{npc}%", f"%{npc}%"] + params,
                )
                for row in cur.fetchall():
                    npc_hits.setdefault(row["character_id"], []).append(row["ts"])

        encounters = _derive_gap_based_encounters(activity, EQL_OUT_OF_COMBAT_SECONDS)

        if npc:
            def _touches_npc(e):
                hits = npc_hits.get(e["character_id"])
                if not hits:
                    return False
                end = e["stop_ts"] or datetime.now()
                idx = bisect.bisect_left(hits, e["start_ts"])
                return idx < len(hits) and hits[idx] <= end
            encounters = [e for e in encounters if _touches_npc(e)]

        encounters.sort(key=lambda e: e["start_ts"], reverse=not ascending)
        if limit:
            encounters = encounters[:limit]

        result = []
        for enc in encounters:
            end = enc["stop_ts"]
            active = end is None
            span = max(((end or datetime.now()) - enc["start_ts"]).total_seconds(), 0.1)
            stats_params = [enc["character_id"], enc["start_ts"]]
            ts_clause = "ts >= %s"
            if end:
                # inclusive: the killing blow / death line is commonly logged
                # at the exact same (second-precision) timestamp as "You stop
                # fighting.", so a strict < would cut off the encounter's own
                # final hit and kill.
                ts_clause += " AND ts <= %s"
                stats_params.append(end)

            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT "
                    f"  SUM(CASE WHEN source_type='you' AND event_type IN {DAMAGE_EVENT_TYPES_SQL} THEN amount ELSE 0 END) AS damage_out, "
                    f"  SUM(CASE WHEN target_type='you' AND event_type IN {DAMAGE_EVENT_TYPES_SQL} THEN amount ELSE 0 END) AS damage_in, "
                    f"  SUM(CASE WHEN source_type='you' AND event_type='spell_heal' THEN amount ELSE 0 END) AS healing_out, "
                    f"  SUM(CASE WHEN target_type='you' AND event_type='spell_heal' THEN amount ELSE 0 END) AS healing_in, "
                    f"  SUM(CASE WHEN source_type='you' AND event_type='spell_heal' THEN CAST(JSON_EXTRACT(extra,'$.overheal') AS UNSIGNED) ELSE 0 END) AS overheal_out, "
                    f"  SUM(CASE WHEN target_type='you' AND event_type='spell_heal' THEN CAST(JSON_EXTRACT(extra,'$.overheal') AS UNSIGNED) ELSE 0 END) AS overheal_in, "
                    f"  SUM(CASE WHEN source_type='you' AND event_type='melee' THEN 1 ELSE 0 END) AS swings, "
                    f"  SUM(CASE WHEN source_type='you' AND event_type='melee' AND outcome IN ('hit','crit') THEN 1 ELSE 0 END) AS landed, "
                    f"  SUM(CASE WHEN source_type='you' AND event_type='melee' AND outcome='crit' THEN 1 ELSE 0 END) AS crits, "
                    # not just kills you personally landed -- see the /zones
                    # query for why (group/raid content, not solo-only)
                    f"  SUM(CASE WHEN event_type='death' AND target_type != 'you' THEN 1 ELSE 0 END) AS kills "
                    f"FROM events WHERE character_id=%s AND {ts_clause}",
                    stats_params,
                )
                s = cur.fetchone()

                # First mob engaged + distinct mob count, for naming the
                # encounter ("a dale snake (+1 more, 2 mobs total)"). Not
                # just your own lines -- group/raid content means a party
                # member (or, once pets are nameable, a pet) can be the one
                # actually trading blows with a mob while you're doing
                # something else for a few seconds. Two-pass: first collect
                # mobs confirmed via your own direct engagement (you vs an
                # article-prefixed generic name is unambiguous either way),
                # then walk the window again and count anyone else's combat
                # against an article-prefixed name OR an already-confirmed
                # mob name as "engaged" too. A proper-noun name (boss, player,
                # or pet) with no direct link to you or a confirmed mob is
                # left alone -- there's no text-only way to tell those apart.
                cur.execute(
                    f"SELECT source_type, source_name, target_type, target_name FROM events "
                    f"WHERE character_id=%s AND {ts_clause} AND event_type IN {DAMAGE_EVENT_TYPES_SQL} "
                    "ORDER BY ts, id",
                    stats_params,
                )
                rows = cur.fetchall()

                confirmed_mobs = set()
                for ev in rows:
                    if ev["source_type"] == "you" and ev["target_name"]:
                        confirmed_mobs.add(ev["target_name"])
                    elif ev["target_type"] == "you" and ev["source_name"]:
                        confirmed_mobs.add(ev["source_name"])

                def _is_mob(actor_type, name):
                    return bool(name) and (actor_type == "npc" or name in confirmed_mobs)

                mob_names, seen_mobs = [], set()
                for ev in rows:
                    for actor_type, name in (
                        (ev["source_type"], ev["source_name"]),
                        (ev["target_type"], ev["target_name"]),
                    ):
                        if _is_mob(actor_type, name) and name not in seen_mobs:
                            seen_mobs.add(name)
                            mob_names.append(name)

            damage_out = float(s["damage_out"] or 0)
            damage_in = float(s["damage_in"] or 0)
            healing_out = float(s["healing_out"] or 0)
            healing_in = float(s["healing_in"] or 0)
            overheal_out = float(s["overheal_out"] or 0)
            overheal_in = float(s["overheal_in"] or 0)
            swings = int(s["swings"] or 0)
            landed = int(s["landed"] or 0)
            crits = int(s["crits"] or 0)
            kills = int(s["kills"] or 0)
            result.append({
                "character": enc["character_name"],
                "game": enc["game"],
                "name": enc.get("name"),
                "start_ts": enc["start_ts"].isoformat(),
                "stop_ts": end.isoformat() if end else None,
                "active": active,
                "duration_s": round(span),
                "damage_out": damage_out,
                "damage_in": damage_in,
                "dps_out": round(damage_out / span, 1),
                "dps_in": round(damage_in / span, 1),
                "healing_out": healing_out,
                "healing_in": healing_in,
                "overheal_out": overheal_out,
                "overheal_in": overheal_in,
                "hit_pct": round(landed / swings * 100, 1) if swings else None,
                "crit_pct": round(crits / landed * 100, 1) if landed else None,
                "kills": kills,
                "first_mob": mob_names[0] if mob_names else None,
                "mob_count": len(mob_names),
            })
    finally:
        conn.close()

    return result


@app.get("/api/encounters")
def api_encounters(character: str = "", start_ts: str = "", end_ts: str = "", npc: str = "", limit: int = 30):
    return JSONResponse(_compute_encounters(character, start_ts, end_ts, npc, limit))


def _known_logs():
    # Powers the log picker on /encounters -- lets you pick a bounded window
    # by log instead of guessing a date range blind. file_mtime comes from
    # the filesystem (matches discovery.py's live-file resolution), not the
    # DB, so it reflects reality even if the tailer's been down a while.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ls.id, ls.file_path, ls.last_parsed_at, ls.live, "
                "c.name AS character_name, g.code AS game, "
                "MIN(e.ts) AS first_event_ts, MAX(e.ts) AS last_event_ts, COUNT(e.id) AS event_count "
                "FROM log_sources ls "
                "JOIN characters c ON ls.character_id=c.id "
                "JOIN games g ON ls.game_id=g.id "
                "LEFT JOIN events e ON e.log_source_id=ls.id "
                "GROUP BY ls.id, ls.file_path, ls.last_parsed_at, ls.live, c.name, g.code"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    logs = []
    for r in rows:
        try:
            file_mtime = datetime.fromtimestamp(os.path.getmtime(r["file_path"]))
        except OSError:
            file_mtime = None
        logs.append({**r, "file_mtime": file_mtime})
    logs.sort(key=lambda l: l["file_mtime"] or datetime.min, reverse=True)
    return logs


@app.get("/encounters", response_class=HTMLResponse)
def encounters_browse(
    request: Request, character: str = "", start_ts: str = "", end_ts: str = "", npc: str = "",
    limit: int = 200, order: str = "desc",
):
    limit = max(0, min(limit, 5000))
    # Defaults to showing nothing until a log + date range is picked -- an
    # unbounded fetch here means pulling every combat event in the DB into
    # Python before merging (already ~240k rows after under a month of
    # play), so "run automatically on page load" doesn't scale.
    ready = bool(character and start_ts and end_ts)
    encounters = []
    totals = {"count": 0, "damage_out": 0, "damage_in": 0, "healing_out": 0, "healing_in": 0, "kills": 0, "duration_s": 0}
    if ready:
        encounters = _compute_encounters(character, start_ts, end_ts, npc, limit, ascending=(order == "asc"))
        totals = {
            "count": len(encounters),
            "damage_out": sum(e["damage_out"] for e in encounters),
            "damage_in": sum(e["damage_in"] for e in encounters),
            "healing_out": sum(e["healing_out"] for e in encounters),
            "healing_in": sum(e["healing_in"] for e in encounters),
            "kills": sum(e["kills"] for e in encounters),
            "duration_s": sum(e["duration_s"] for e in encounters),
        }

    return templates.TemplateResponse(request, "encounters.html", {
        "encounters": encounters,
        "totals": totals,
        "ready": ready,
        "known_logs": _known_logs(),
        "filters": {"character": character, "start_ts": start_ts, "end_ts": end_ts, "npc": npc, "limit": limit, "order": order},
    })


def _compute_encounter_combat_breakdown(windows, label_self_as_you=True):
    """Full breakdown across one or more (character, start_ts, stop_ts[, npc])
    windows: every attack type (melee verb or spell name), split by
    combatant vs NPC and by direction (combatant attacking the NPC, or the
    NPC attacking the combatant) -- hit/crit rate, evasion, resist rate,
    min/avg/max damage per verb (the standalone /attacks page used to show
    this unscoped to any encounter; removed 2026-08-24 since the numbers
    meant nothing without knowing which fight they came from -- this is
    the same data, scoped to the given window(s) instead of just to 'you').
    Each window is OR'd into a single query, so rows from several (possibly
    disjoint, possibly different-character) windows land in the same
    verb/npc buckets and merge for free -- this is what powers both a
    single encounter's breakdown (one window) and a merged multi-encounter
    breakdown (several windows).

    A window may optionally carry an 'npc' filter (case-insensitive) -- used
    when the caller cherry-picked one specific mob out of an encounter
    rather than the whole thing (see /encounters' per-mob checkboxes). A row
    is kept if it falls in *any* window's character+time range AND, when
    that window has an npc filter, the row's mob-side name matches it; an
    unfiltered window covering the same range always lets the row through.

    Same two-pass mob-vs-combatant resolution as _compute_encounters/
    api_encounter_detail: confirm mobs via your own engagement first (since
    a proper-named boss and a player name are textually identical), then use
    that plus unambiguous article-prefixed NPC names to sort everyone else's
    lines into combatant vs NPC too. EQL is combat-locked to your own
    group/raid, so anyone showing up this way is legitimately part of it.

    label_self_as_you: when True (the single-encounter case), the window's
    own log-owner is labelled "You". When merging windows that may belong to
    different characters, that label would collide between them, so the
    caller passes False and each log-owner is labelled by their real
    character name instead.

    Returns (verb_rows, npc_totals, totals_rows): verb_rows is the flat
    per-(combatant, npc, verb, direction) list; npc_totals is per-NPC melee/
    spell damage aggregated across every combatant, keyed by NPC name;
    totals_rows is the same per-verb data but merged across NPCs too --
    per-(combatant, verb, direction), for a single "whole thing merged"
    outgoing/incoming view instead of one broken out per mob."""
    if not windows:
        return [], {}, []

    clauses, params = [], []
    for w in windows:
        clause = "(c.name = %s AND ev.ts >= %s"
        wparams = [w["character"], w["start_ts"]]
        if w.get("stop_ts"):
            clause += " AND ev.ts <= %s"
            wparams.append(w["stop_ts"])
        clause += ")"
        clauses.append(clause)
        params.extend(wparams)
    where = " OR ".join(clauses)

    # Parsed once for the per-row npc-filter check below, rather than
    # re-parsing each window's timestamps for every row.
    parsed_windows = [
        {
            "character": w["character"],
            "start_dt": datetime.fromisoformat(w["start_ts"]),
            "stop_dt": datetime.fromisoformat(w["stop_ts"]) if w.get("stop_ts") else None,
            "npc": (w.get("npc") or "").lower() or None,
        }
        for w in windows
    ]

    def row_allowed(character_name, ts, npc_name):
        npc_lower = npc_name.lower()
        for w in parsed_windows:
            if w["character"] != character_name:
                continue
            if ts < w["start_dt"] or (w["stop_dt"] is not None and ts > w["stop_dt"]):
                continue
            if w["npc"] is not None and w["npc"] != npc_lower:
                continue
            return True
        return False

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.ts, ev.source_name, ev.source_type, ev.target_name, ev.target_type, "
                "ev.event_type, ev.verb, ev.amount, ev.outcome, c.name AS character_name "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE ({where}) "
                "AND ev.event_type IN ('melee','spell_damage','ability_damage','spell_resist') "
                "AND ev.verb IS NOT NULL",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    # Matched/grouped case-insensitively throughout this function: the same
    # mob's name can appear both sentence-initial-capitalized ("Orc
    # centurion hits YOU...") and mid-sentence-lowercase ("You pierce orc
    # centurion...") -- confirmed real. normalize_actor_name() at ingest
    # only catches this for the "A"/"An" leading-article case; a no-article
    # name like this one isn't caught there (nothing to anchor the regex
    # on), so without this it wouldn't just cosmetically split into two
    # breakdown blocks -- an unconfirmed casing could fail the is_mob()
    # check entirely and get silently dropped as "neither side reads as a
    # mob". npc_display picks one casing per real mob (whichever is seen
    # first) so buckets/npc_totals below stay keyed consistently.
    confirmed_mobs = set()
    for ev in rows:
        if ev["source_type"] == "you" and ev["target_name"]:
            confirmed_mobs.add(ev["target_name"].lower())
        elif ev["target_type"] == "you" and ev["source_name"]:
            confirmed_mobs.add(ev["source_name"].lower())

    def is_mob(actor_type, name):
        return bool(name) and (actor_type == "npc" or name.lower() in confirmed_mobs)

    npc_display: dict[str, str] = {}

    def canonical_npc(name):
        return npc_display.setdefault(name.lower(), name)

    def new_bucket(**identity):
        return {
            **identity,
            "attempts": 0, "landed": 0, "crits": 0, "misses": 0,
            "dodged": 0, "parried": 0, "blocked": 0, "riposted": 0, "resisted": 0, "absorbed": 0,
            "hit_amounts": [], "crit_amounts": [],
        }

    # Per-(combatant, npc, verb, direction), for the single-encounter/
    # per-mob view, and per-(combatant, verb, direction) -- the same data
    # merged across every NPC -- for the "whole thing merged" totals view.
    buckets: dict[tuple, dict] = {}
    totals_buckets: dict[tuple, dict] = {}

    def bucket(combatant, npc, verb, direction):
        key = (combatant, npc, verb, direction)
        return buckets.setdefault(key, new_bucket(combatant=combatant, npc=npc, verb=verb, direction=direction))

    def totals_bucket(combatant, verb, direction):
        key = (combatant, verb, direction)
        return totals_buckets.setdefault(key, new_bucket(combatant=combatant, verb=verb, direction=direction))

    # NPC-level totals: melee vs spell, aggregated across every combatant --
    # "how much did the whole group do to this NPC" / "how much did the NPC
    # do to the whole group", independent of who specifically dealt/took it.
    npc_totals: dict[str, dict] = {}

    def npc_total(npc):
        return npc_totals.setdefault(npc, {"melee_to": 0, "spell_to": 0, "melee_from": 0, "spell_from": 0})

    for ev in rows:
        s_type, s_name = ev["source_type"], ev["source_name"]
        t_type, t_name = ev["target_type"], ev["target_name"]
        if not s_name or not t_name:
            continue
        s_is_mob, t_is_mob = is_mob(s_type, s_name), is_mob(t_type, t_name)
        amount = ev["amount"] or 0
        is_melee = ev["event_type"] == "melee"
        if t_is_mob and not s_is_mob:
            combatant = ("You" if label_self_as_you else ev["character_name"]) if s_type == "you" else s_name
            npc, direction = canonical_npc(t_name), "out"
        elif s_is_mob and not t_is_mob:
            combatant = ("You" if label_self_as_you else ev["character_name"]) if t_type == "you" else t_name
            npc, direction = canonical_npc(s_name), "in"
        else:
            continue  # both or neither side reads as a mob -- too ambiguous to place

        if not row_allowed(ev["character_name"], ev["ts"], npc):
            continue  # excluded by an npc-scoped window (a cherry-picked mob, not this one)

        nt = npc_total(npc)
        nt["melee_to" if (is_melee and direction == "out") else
           "spell_to" if direction == "out" else
           "melee_from" if is_melee else "spell_from"] += amount

        outcome = ev["outcome"]
        for b in (bucket(combatant, npc, ev["verb"], direction), totals_bucket(combatant, ev["verb"], direction)):
            b["attempts"] += 1
            if outcome in ("hit", "crit"):
                b["landed"] += 1
                if ev["amount"]:
                    b["crit_amounts" if outcome == "crit" else "hit_amounts"].append(ev["amount"])
                if outcome == "crit":
                    b["crits"] += 1
            elif outcome == "miss":
                b["misses"] += 1
            elif outcome == "dodge":
                b["dodged"] += 1
            elif outcome == "parry":
                b["parried"] += 1
            elif outcome == "block":
                b["blocked"] += 1
            elif outcome == "riposte":
                b["riposted"] += 1
            elif outcome == "resist":
                b["resisted"] += 1
            elif outcome == "absorb":
                b["absorbed"] += 1

    def pct(n, den):
        return round(n / den * 100, 1) if den else None

    def shape(b):
        attempts, landed = b["attempts"], b["landed"]
        hit_amts, crit_amts = b["hit_amounts"], b["crit_amounts"]
        row = {k: v for k, v in b.items() if k not in (
            "attempts", "landed", "crits", "misses", "dodged", "parried",
            "blocked", "riposted", "resisted", "absorbed", "hit_amounts", "crit_amounts",
        )}
        row.update({
            "total_damage": sum(hit_amts) + sum(crit_amts),
            "attempts": attempts,
            "hit_pct": pct(landed, attempts),
            "crit_pct": pct(b["crits"], landed),
            "miss_pct": pct(b["misses"], attempts),
            "dodge_pct": pct(b["dodged"], attempts),
            "parry_pct": pct(b["parried"], attempts),
            "block_pct": pct(b["blocked"], attempts),
            "riposte_pct": pct(b["riposted"], attempts),
            "resist_pct": pct(b["resisted"], attempts),
            "absorb_pct": pct(b["absorbed"], attempts),
            "min_hit": min(hit_amts) if hit_amts else None,
            "avg_hit": round(sum(hit_amts) / len(hit_amts), 1) if hit_amts else None,
            "max_hit": max(hit_amts) if hit_amts else None,
            "min_crit": min(crit_amts) if crit_amts else None,
            "avg_crit": round(sum(crit_amts) / len(crit_amts), 1) if crit_amts else None,
            "max_crit": max(crit_amts) if crit_amts else None,
        })
        return row

    verb_rows = [shape(b) for b in buckets.values()]
    verb_rows.sort(key=lambda r: (r["npc"], r["combatant"], r["direction"], -r["attempts"]))
    totals_rows = [shape(b) for b in totals_buckets.values()]
    totals_rows.sort(key=lambda r: (r["combatant"], r["direction"], -r["attempts"]))
    return verb_rows, npc_totals, totals_rows


def _compute_encounter_healing_breakdown(windows, label_self_as_you=True):
    """Per-(healer, spell) heal breakdown across one or more windows -- same
    window-OR'ing and You-labeling convention as
    _compute_encounter_combat_breakdown. Unlike the damage breakdown this
    isn't split by target: the ask was "who healed with what spell for how
    much", not a per-target table, so casts of the same spell by the same
    healer land in one bucket regardless of who they targeted.

    "effective" is `amount` -- what the target actually gained, already
    capped by their missing HP -- and "raw"/"overheal" come from the
    `extra` JSON's potential/overheal (see _heal_extra in eq_legends.py);
    raw with no cap hit just equals amount, so overheal is 0. Returns a
    flat list of per-(healer, spell) dicts; the caller aggregates per healer
    and computes percentages."""
    if not windows:
        return []

    clauses, params = [], []
    for w in windows:
        clause = "(c.name = %s AND ev.ts >= %s"
        wparams = [w["character"], w["start_ts"]]
        if w.get("stop_ts"):
            clause += " AND ev.ts <= %s"
            wparams.append(w["stop_ts"])
        clause += ")"
        clauses.append(clause)
        params.extend(wparams)
    where = " OR ".join(clauses)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.source_name, ev.source_type, ev.verb AS spell, ev.amount, ev.outcome, ev.extra, "
                "c.name AS character_name "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE ({where}) AND ev.event_type='spell_heal' AND ev.verb IS NOT NULL",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    buckets: dict[tuple, dict] = {}

    def bucket(healer, spell):
        return buckets.setdefault((healer, spell), {
            "healer": healer, "spell": spell,
            "casts": 0, "crits": 0, "effective": 0, "raw": 0, "overheal": 0,
        })

    for ev in rows:
        healer = ("You" if label_self_as_you else ev["character_name"]) if ev["source_type"] == "you" else ev["source_name"]
        extra = json.loads(ev["extra"]) if ev["extra"] else {}
        amount = ev["amount"] or 0

        b = bucket(healer, ev["spell"])
        b["casts"] += 1
        if ev["outcome"] == "crit":
            b["crits"] += 1
        b["effective"] += amount
        b["raw"] += extra.get("potential", amount)
        b["overheal"] += extra.get("overheal", 0)

    return list(buckets.values())


def _compute_encounter_procs(windows):
    """Proc-trigger counts (event_type='proc_trigger', see h_proc_trigger in
    eq_legends.py) and rune instances (rune_buff_start...rune_buff_end, with
    rune_gain top-ups accumulated in between -- see h_rune_buff_start/
    h_rune_gain/h_rune_buff_end), across one or more windows -- same
    window-OR'ing convention as _compute_encounter_combat_breakdown.
    Self-only events (no target/npc), so no per-mob split. Lets a named
    "Encounter Start daggers"-style run (see [[project_encounter_markers]])
    be compared against another gear loadout for proc rate and rune
    size/uptime, not just damage.

    Confirmed real (2026-08-24): "gain a rune for N points" fires far more
    often than the buff icon actually (re)applying or breaking -- most
    ticks just top up an already-active rune rather than starting a new
    one, and a rune can persist across the gap between two encounters
    (confirmed real: 7 top-ups landed in a window whose own rune_buff_start
    was several minutes earlier, in the previous pull). So each window is
    seeded with whatever rune was already open as of its own start_ts
    (looked up separately below) before pairing the in-window
    start/gain/end events sequentially per character. A gain with no rune
    open even after seeding (confirmed real: a burst of ticks with no
    rune_buff_start nearby at all -- turned out to be a Focus-effect item
    "shimmers briefly" pulse, not a rune source, so deliberately not
    attributed to it) lands in an "Unknown source" bucket instead of being
    dropped -- see the rune_gain branch below."""
    if not windows:
        return {"procs": [], "rune_instances": []}

    clauses, params = [], []
    for w in windows:
        clause = "(c.name = %s AND ev.ts >= %s"
        wparams = [w["character"], w["start_ts"]]
        if w.get("stop_ts"):
            clause += " AND ev.ts <= %s"
            wparams.append(w["stop_ts"])
        clause += ")"
        clauses.append(clause)
        params.extend(wparams)
    where = " OR ".join(clauses)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.target_name AS proc, COUNT(*) AS triggers "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE ({where}) AND ev.event_type='proc_trigger' "
                "GROUP BY ev.target_name ORDER BY triggers DESC",
                params,
            )
            procs = cur.fetchall()

            cur.execute(
                "SELECT ev.character_id, ev.ts, ev.event_type, ev.target_name AS tier, ev.amount "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE ({where}) "
                "AND ev.event_type IN ('rune_buff_start','rune_gain','rune_buff_end') "
                "ORDER BY ev.character_id, ev.ts",
                params,
            )
            rune_events = cur.fetchall()

            # A rune can already be active when a window starts (confirmed
            # real: it can persist across the gap between two encounters,
            # getting silently topped up the whole time) -- its own
            # rune_buff_start then falls before this window and would
            # otherwise never be seen, leaving every in-window top-up
            # orphaned with no instance to attribute to. Seed each window's
            # character with whatever rune_buff_start most recently
            # preceded it, unless a rune_buff_end already closed it first.
            seed_by_char: dict[int, dict] = {}
            for w in windows:
                cur.execute(
                    "SELECT ev.character_id, ev.ts, ev.event_type, ev.target_name AS tier "
                    "FROM events ev JOIN characters c ON ev.character_id=c.id "
                    "WHERE c.name=%s AND ev.ts <= %s "
                    "AND ev.event_type IN ('rune_buff_start','rune_buff_end') "
                    "ORDER BY ev.ts DESC LIMIT 1",
                    (w["character"], w["start_ts"]),
                )
                row = cur.fetchone()
                if row and row["event_type"] == "rune_buff_start":
                    seed_by_char[row["character_id"]] = {
                        "tier": row["tier"], "start_ts": row["ts"], "end_ts": None,
                        "gain_count": 0, "total": 0,
                    }
    finally:
        conn.close()

    # Confirmed real (2026-08-24): at 1s log resolution, "You gain a rune
    # for N points" and "A coat of shimmering runes surrounds you." can
    # share the exact same timestamp with the gain line printed FIRST in
    # the raw log -- SQL's ORDER BY ts alone doesn't guarantee a stable tie-
    # break, and the physical log order would otherwise have the gain
    # arrive with no instance open yet to attribute it to. Re-sorted here
    # so same-tick events always resolve start-before-gain-before-end,
    # regardless of raw row order.
    _RUNE_EVENT_PRIORITY = {"rune_buff_start": 0, "rune_gain": 1, "rune_buff_end": 2}
    rune_events = sorted(
        rune_events,
        key=lambda e: (e["character_id"], e["ts"], _RUNE_EVENT_PRIORITY[e["event_type"]]),
    )

    rune_instances = []
    open_by_char: dict[int, dict] = seed_by_char
    for ev in rune_events:
        cid = ev["character_id"]
        if ev["event_type"] == "rune_buff_start":
            if cid in open_by_char:  # defensive: two starts with no fade between them
                rune_instances.append(open_by_char.pop(cid))
            open_by_char[cid] = {
                "tier": ev["tier"], "start_ts": ev["ts"], "end_ts": None,
                "gain_count": 0, "total": 0,
            }
        elif ev["event_type"] == "rune_gain":
            inst = open_by_char.get(cid)
            if inst is None:
                # No known rune-tier instance open, even after seeding --
                # confirmed real (2026-08-24): a burst of "gain a rune"
                # ticks with no rune_buff_start anywhere nearby, preceded
                # instead by "<Item> (Exaltation) shimmers briefly." (a
                # Focus effect pulsing, e.g. "Djarn's Amethyst Ring") --
                # deliberately NOT attributed to that item, since a Focus
                # effect firing isn't the same thing as it being the rune's
                # actual source (unconfirmed link, same reasoning as not
                # linking rune_gain to Rampage). Bucketed here instead of
                # dropped, so the real point total isn't silently lost.
                inst = open_by_char.setdefault(cid, {
                    "tier": "Unknown source", "start_ts": ev["ts"], "end_ts": None,
                    "gain_count": 0, "total": 0,
                })
            inst["gain_count"] += 1
            inst["total"] += ev["amount"] or 0
        elif ev["event_type"] == "rune_buff_end":
            inst = open_by_char.pop(cid, None)
            if inst is not None:
                inst["end_ts"] = ev["ts"]
                rune_instances.append(inst)
    rune_instances.extend(open_by_char.values())  # still active at window end

    for inst in rune_instances:
        inst["duration_s"] = round((inst["end_ts"] - inst["start_ts"]).total_seconds()) if inst["end_ts"] else None
    rune_instances.sort(key=lambda i: i["start_ts"])
    return {"procs": procs, "rune_instances": rune_instances}


def _build_encounter_healing(windows, label_self_as_you, duration_s):
    rows = _compute_encounter_healing_breakdown(windows, label_self_as_you)

    def pct(n, den):
        return round(n / den * 100, 1) if den else None

    def stat_row(name, casts, crits, effective, raw, overheal):
        return {
            "name": name, "casts": casts,
            "crit_pct": pct(crits, casts),
            "total_heal": raw, "effective_heal": effective, "overheal": overheal,
            "efficiency_pct": pct(effective, raw),
            "hps": round(effective / duration_s, 1),
        }

    by_healer: dict[str, list] = {}
    for r in rows:
        by_healer.setdefault(r["healer"], []).append(r)

    healers = []
    for healer, spells in by_healer.items():
        spell_rows = [
            stat_row(s["spell"], s["casts"], s["crits"], s["effective"], s["raw"], s["overheal"])
            for s in sorted(spells, key=lambda s: -s["effective"])
        ]
        summary = stat_row(
            healer,
            sum(s["casts"] for s in spells), sum(s["crits"] for s in spells),
            sum(s["effective"] for s in spells), sum(s["raw"] for s in spells), sum(s["overheal"] for s in spells),
        )
        summary["spells"] = spell_rows
        healers.append(summary)
    healers.sort(key=lambda h: -h["effective_heal"])
    return healers


def _build_encounter_breakdown_npcs(windows, label_self_as_you, split_you, duration_s):
    """Shared by the single-encounter and merged-encounters breakdown routes.
    split_you controls whether the log-owner's own rows get pulled into a
    dedicated "You" section (single-encounter view) or just sit in the
    combatant list with everyone else (merged view, where "own" isn't a
    single well-defined combatant once several characters are merged)."""
    verb_rows, npc_totals, _ = _compute_encounter_combat_breakdown(windows, label_self_as_you)

    # combatant -> direction -> [attack-type rows], per NPC
    by_npc_combatant: dict[str, dict[str, dict[str, list]]] = {}
    for r in verb_rows:
        combatants = by_npc_combatant.setdefault(r["npc"], {})
        combatants.setdefault(r["combatant"], {"out": [], "in": []})[r["direction"]].append(r)

    def combatant_summary(name, dirs):
        out_total = sum(r["total_damage"] for r in dirs["out"])
        in_total = sum(r["total_damage"] for r in dirs["in"])
        return {
            "combatant": name,
            "out": dirs["out"], "in": dirs["in"],
            "out_total": out_total, "in_total": in_total,
            "out_dps": round(out_total / duration_s, 1),
            "in_dps": round(in_total / duration_s, 1),
        }

    npcs = []
    for npc_name, combatants in by_npc_combatant.items():
        you_dirs = combatants.pop("You", None) if split_you else None
        others = sorted(
            (combatant_summary(name, dirs) for name, dirs in combatants.items()),
            key=lambda c: -c["out_total"],
        )
        nt = npc_totals.get(npc_name, {"melee_to": 0, "spell_to": 0, "melee_from": 0, "spell_from": 0})
        npcs.append({
            "npc": npc_name,
            "melee_damage_to": nt["melee_to"], "spell_damage_to": nt["spell_to"],
            "melee_damage_from": nt["melee_from"], "spell_damage_from": nt["spell_from"],
            "you": combatant_summary("You", you_dirs) if you_dirs is not None else None,
            "others": others,
        })
    npcs.sort(key=lambda n: -(n["melee_damage_to"] + n["spell_damage_to"]))
    return npcs


def _build_encounter_damage_totals(totals_rows, duration_s):
    """The merged-encounters counterpart to _build_encounter_breakdown_npcs:
    same per-(combatant, verb, direction) rows, but summed across every NPC
    into one outgoing/incoming total per combatant instead of one block per
    mob -- for a "whole thing merged" view once several fights (or several
    cherry-picked mobs) are combined and a per-mob breakdown isn't the
    point."""
    def by_direction(direction):
        by_combatant: dict[str, list] = {}
        for r in totals_rows:
            if r["direction"] == direction:
                by_combatant.setdefault(r["combatant"], []).append(r)
        combatants = []
        for name, attacks in by_combatant.items():
            total_damage = sum(a["total_damage"] for a in attacks)
            combatants.append({
                "name": name,
                "total_damage": total_damage,
                "attempts": sum(a["attempts"] for a in attacks),
                "dps": round(total_damage / duration_s, 1),
                "attacks": sorted(attacks, key=lambda a: -a["total_damage"]),
            })
        combatants.sort(key=lambda c: -c["total_damage"])
        return combatants

    return {"out": by_direction("out"), "in": by_direction("in")}


@app.get("/encounters/breakdown", response_class=HTMLResponse)
def encounter_breakdown(request: Request, character: str, start_ts: str, stop_ts: str = ""):
    start_dt = datetime.fromisoformat(start_ts)
    end_dt = datetime.fromisoformat(stop_ts) if stop_ts else datetime.now()
    duration_s = max(round((end_dt - start_dt).total_seconds()), 1)

    windows = [{"character": character, "start_ts": start_ts, "stop_ts": stop_ts}]
    npcs = _build_encounter_breakdown_npcs(windows, label_self_as_you=True, split_you=True, duration_s=duration_s)
    healers = _build_encounter_healing(windows, label_self_as_you=True, duration_s=duration_s)
    procs = _compute_encounter_procs(windows)

    return templates.TemplateResponse(request, "encounter_breakdown.html", {
        "npcs": npcs,
        "damage_totals": None,
        "healers": healers,
        "procs": procs["procs"],
        "rune_instances": procs["rune_instances"],
        "merged": False,
        "windows": None,
        "start_ts": start_ts,
        "stop_ts": stop_ts or end_dt.isoformat(timespec="seconds"),
        "duration_s": duration_s,
        "filters": {"character": character, "start_ts": start_ts, "stop_ts": stop_ts},
    })


@app.get("/encounters/breakdown/merged", response_class=HTMLResponse)
def encounter_breakdown_merged(
    request: Request,
    character: list[str] = Query(...),
    start_ts: list[str] = Query(...),
    stop_ts: list[str] = Query(default=[]),
    npc: list[str] = Query(default=[]),
):
    # Aligned by index -- one (character, start_ts, stop_ts, npc) tuple per
    # selected item. Missing/blank stop_ts/npc entries pad out to an
    # open-ended window / no-mob-filter respectively, same as the
    # single-encounter route's stop_ts default. A blank npc means "the whole
    # encounter"; a set npc means one specific mob was cherry-picked out of
    # it (via the per-mob checkboxes on /encounters and /live).
    stop_ts = stop_ts + [""] * (len(character) - len(stop_ts))
    npc = npc + [""] * (len(character) - len(npc))
    windows = [
        {"character": c, "start_ts": s, "stop_ts": e or None, "npc": n or None}
        for c, s, e, n in zip(character, start_ts, stop_ts, npc)
    ]
    if not windows:
        raise HTTPException(400, "at least one encounter is required")

    # Damage is scoped per-selection (an npc-filtered window only lets that
    # mob's rows through), but duration/healing aren't npc-scoped -- heals
    # don't have a mob side, and two mob selections that share the same
    # encounter time range shouldn't double-count that time. So duration and
    # healing use the distinct (character, start, stop) ranges only, with
    # any npc filters collapsed out.
    seen_ranges = set()
    unique_time_windows = []
    for w in windows:
        key = (w["character"], w["start_ts"], w["stop_ts"])
        if key not in seen_ranges:
            seen_ranges.add(key)
            unique_time_windows.append({"character": w["character"], "start_ts": w["start_ts"], "stop_ts": w["stop_ts"]})

    # Combined duration is the sum of each distinct range's own span, not
    # the wall-clock distance between the first and last -- these are
    # disjoint fights merged together, and DPS should reflect only the time
    # actually spent in them.
    duration_s = 0.0
    for w in unique_time_windows:
        start_dt = datetime.fromisoformat(w["start_ts"])
        end_dt = datetime.fromisoformat(w["stop_ts"]) if w["stop_ts"] else datetime.now()
        duration_s += max((end_dt - start_dt).total_seconds(), 0.1)
    duration_s = max(round(duration_s), 1)

    _, _, totals_rows = _compute_encounter_combat_breakdown(windows, label_self_as_you=False)
    damage_totals = _build_encounter_damage_totals(totals_rows, duration_s)
    healers = _build_encounter_healing(unique_time_windows, label_self_as_you=False, duration_s=duration_s)
    procs = _compute_encounter_procs(unique_time_windows)

    return templates.TemplateResponse(request, "encounter_breakdown.html", {
        "npcs": [],
        "damage_totals": damage_totals,
        "healers": healers,
        "procs": procs["procs"],
        "rune_instances": procs["rune_instances"],
        "merged": True,
        "windows": windows,
        "start_ts": windows[0]["start_ts"],
        "stop_ts": windows[-1]["stop_ts"] or "",
        "duration_s": duration_s,
        "filters": {"character": "", "start_ts": "", "stop_ts": ""},
    })


@app.get("/api/encounters/detail")
def api_encounter_detail(character: str, start_ts: str, stop_ts: str = ""):
    # Per-mob breakdown for one encounter -- fetched on demand (only when
    # expanded in the UI) rather than eagerly for every encounter in the
    # list, since that's cheap for one encounter's handful of events but
    # wasteful multiplied across dozens of list rows. Aggregated in Python
    # since per-encounter event volume is small and it avoids a much uglier
    # multi-way-pivot SQL query.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            clauses = [
                "c.name = %s", "ev.ts >= %s",
                f"ev.event_type IN {DAMAGE_EVENT_TYPES_SQL[:-1]},'spell_resist')",
            ]
            params = [character, start_ts]
            if stop_ts:
                clauses.append("ev.ts <= %s")  # inclusive -- see api_encounters for why
                params.append(stop_ts)
            cur.execute(
                "SELECT ev.source_name, ev.source_type, ev.target_name, ev.target_type, "
                "ev.event_type, ev.amount, ev.outcome, ev.extra "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE {' AND '.join(clauses)}",
                params,
            )
            events = cur.fetchall()
    finally:
        conn.close()

    def _is_damage_shield(ev):
        if not ev["extra"]:
            return False
        return json.loads(ev["extra"]).get("damage_shield", False)

    mobs: dict[str, dict] = {}
    _EVADE_OUTCOMES = ("dodge", "parry", "block", "riposte")

    def mob_row(name):
        return mobs.setdefault(name, {
            "mob": name, "damage_out": 0, "damage_in": 0,
            "swings": 0, "landed": 0, "crits": 0,
            "their_swings": 0, "their_landed": 0, "their_crits": 0, "evaded": 0,
            "spell_attempts": 0, "resisted": 0, "ds_out": 0, "ds_in": 0,
        })

    for ev in events:
        is_ds = _is_damage_shield(ev)
        if ev["source_type"] == "you" and ev["target_name"]:
            row = mob_row(ev["target_name"])
            if ev["amount"]:
                row["damage_out"] += ev["amount"]
                if is_ds:
                    row["ds_out"] += ev["amount"]
            if ev["event_type"] == "melee":
                row["swings"] += 1
                if ev["outcome"] in ("hit", "crit"):
                    row["landed"] += 1
                if ev["outcome"] == "crit":
                    row["crits"] += 1
            elif ev["event_type"] in ("spell_damage", "spell_resist") and not is_ds:
                # damage shield ticks are automatic reflect damage, not a
                # targeted spell cast -- excluded so they don't dilute resist%
                row["spell_attempts"] += 1
                if ev["outcome"] == "resist":
                    row["resisted"] += 1
        if ev["target_type"] == "you" and ev["source_name"]:
            row = mob_row(ev["source_name"])
            if ev["amount"]:
                row["damage_in"] += ev["amount"]
                if is_ds:
                    row["ds_in"] += ev["amount"]
            if ev["event_type"] == "melee":
                row["their_swings"] += 1
                if ev["outcome"] in ("hit", "crit"):
                    row["their_landed"] += 1
                if ev["outcome"] == "crit":
                    row["their_crits"] += 1
                if ev["outcome"] in _EVADE_OUTCOMES:
                    row["evaded"] += 1

    # Contributor breakdown per mob -- not just your own damage, but anyone
    # else's (party member, or eventually a named pet) who also fought it.
    # "Mob" confirmed above via your own engagement is reused here as the
    # anchor: a proper-noun name only counts as the mob side if it's already
    # confirmed, or is an unambiguous article-prefixed generic name -- same
    # reasoning as the encounter-level mob detection.
    confirmed_mobs = set(mobs.keys())

    def _is_mob(actor_type, name):
        return bool(name) and (actor_type == "npc" or name in confirmed_mobs)

    contributors: dict[str, dict[str, int]] = {}
    for ev in events:
        if not ev["amount"]:
            continue
        s_type, s_name, t_type, t_name = ev["source_type"], ev["source_name"], ev["target_type"], ev["target_name"]
        if s_name and t_name and _is_mob(t_type, t_name) and not _is_mob(s_type, s_name):
            actor = "You" if s_type == "you" else s_name
            contributors.setdefault(t_name, {})
            contributors[t_name][actor] = contributors[t_name].get(actor, 0) + ev["amount"]

    result = []
    for row in mobs.values():
        row_contributors = contributors.get(row["mob"], {})
        result.append({
            "mob": row["mob"],
            "damage_out": row["damage_out"],
            "damage_in": row["damage_in"],
            "ds_out": row["ds_out"],
            "ds_in": row["ds_in"],
            "contributors": [
                {"name": name, "damage_out": dmg}
                for name, dmg in sorted(row_contributors.items(), key=lambda kv: -kv[1])
            ],
            "hit_pct": round(row["landed"] / row["swings"] * 100, 1) if row["swings"] else None,
            "crit_pct": round(row["crits"] / row["landed"] * 100, 1) if row["landed"] else None,
            "their_hit_pct": round(row["their_landed"] / row["their_swings"] * 100, 1) if row["their_swings"] else None,
            "their_crit_pct": round(row["their_crits"] / row["their_landed"] * 100, 1) if row["their_landed"] else None,
            # your evasion against this mob's attacks (dodge/parry/block/riposte)
            "evasion_pct": round(row["evaded"] / row["their_swings"] * 100, 1) if row["their_swings"] else None,
            # this mob resisting your spells -- resisted / (landed + resisted)
            "resist_pct": round(row["resisted"] / row["spell_attempts"] * 100, 1) if row["spell_attempts"] else None,
        })
    result.sort(key=lambda r: r["damage_out"], reverse=True)
    return JSONResponse(result)


@app.get("/api/encounters/log")
def api_encounter_log(character: str, start_ts: str, stop_ts: str = "", mob: str = ""):
    # The filtered "combat log" for one encounter (or one mob within it) --
    # sourced from `events`, not raw_lines, so it's inherently just the
    # already-parsed/relevant lines (chat and skill-up noise never became
    # events in the first place). Same-named mobs can't be told apart from
    # text alone, so a mob filter matches every instance of that name.
    clauses = ["c.name = %s", "ev.ts >= %s"]
    params = [character, start_ts]
    if stop_ts:
        clauses.append("ev.ts <= %s")
        params.append(stop_ts)
    if mob:
        clauses.append("(ev.source_name = %s OR ev.target_name = %s)")
        params.extend([mob, mob])

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.ts, ev.raw_line FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE {' AND '.join(clauses)} ORDER BY ev.ts, ev.id",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    return JSONResponse([{"ts": r["ts"].isoformat(), "raw_line": r["raw_line"]} for r in rows])


@app.get("/live", response_class=HTMLResponse)
def live(request: Request):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(id) AS max_id FROM events")
            max_id = cur.fetchone()["max_id"] or 0
            cur.execute(
                "SELECT target_name FROM events WHERE event_type='zone_change' AND source_type='you' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            current_zone = row["target_name"] if row else None
    finally:
        conn.close()
    return templates.TemplateResponse(request, "live.html", {"start_id": max_id, "current_zone": current_zone})


@app.get("/alerts", response_class=HTMLResponse)
def alerts_list(request: Request):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ar.*, g.code AS game_code FROM alert_rules ar "
                "LEFT JOIN games g ON ar.game_id=g.id ORDER BY ar.id DESC"
            )
            rules = cur.fetchall()
            for r in rules:
                r["reaction_types_list"] = (r["reaction_types"] or "").split(",")
                r["reaction_config_dict"] = json.loads(r["reaction_config"]) if r["reaction_config"] else {}
            cur.execute(
                "SELECT al.*, ar.name AS rule_name FROM alert_log al "
                "JOIN alert_rules ar ON al.rule_id=ar.id ORDER BY al.ts DESC LIMIT 100"
            )
            log = cur.fetchall()
            cur.execute("SELECT id, code FROM games")
            games = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "alerts.html", {"rules": rules, "log": log, "games": games})


def _reaction_config(sound_file: str, overlay_text: str, duration_seconds: str, countdown: bool) -> dict:
    reaction_config = {}
    if sound_file:
        reaction_config["sound_file"] = sound_file
    if overlay_text:
        reaction_config["overlay_text"] = overlay_text
    if duration_seconds:
        reaction_config["duration_seconds"] = int(duration_seconds)
    if countdown:
        reaction_config["countdown"] = True
    return reaction_config


@app.post("/alerts/create")
def alerts_create(
    name: str = Form(...), description: str = Form(""), game_id: str = Form(""),
    match_type: str = Form(...), pattern: str = Form(...),
    reaction_types: list[str] = Form([]), sound_file: str = Form(""),
    overlay_text: str = Form(""), duration_seconds: str = Form(""),
    countdown: bool = Form(False), cooldown_seconds: int = Form(0),
):
    reaction_config = _reaction_config(sound_file, overlay_text, duration_seconds, countdown)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_rules (game_id, name, description, match_type, pattern, "
                "reaction_types, reaction_config, cooldown_seconds, enabled) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)",
                (
                    int(game_id) if game_id else None, name, description, match_type, pattern,
                    ",".join(reaction_types), json.dumps(reaction_config) if reaction_config else None,
                    cooldown_seconds,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{rule_id}/update")
def alerts_update(
    rule_id: int,
    name: str = Form(...), description: str = Form(""), game_id: str = Form(""),
    match_type: str = Form(...), pattern: str = Form(...),
    reaction_types: list[str] = Form([]), sound_file: str = Form(""),
    overlay_text: str = Form(""), duration_seconds: str = Form(""),
    countdown: bool = Form(False), cooldown_seconds: int = Form(0),
):
    reaction_config = _reaction_config(sound_file, overlay_text, duration_seconds, countdown)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE alert_rules SET game_id=%s, name=%s, description=%s, match_type=%s, pattern=%s, "
                "reaction_types=%s, reaction_config=%s, cooldown_seconds=%s WHERE id=%s",
                (
                    int(game_id) if game_id else None, name, description, match_type, pattern,
                    ",".join(reaction_types), json.dumps(reaction_config) if reaction_config else None,
                    cooldown_seconds, rule_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{rule_id}/toggle")
def alerts_toggle(rule_id: int):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE alert_rules SET enabled = NOT enabled WHERE id=%s", (rule_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/alerts", status_code=303)


@app.post("/alerts/{rule_id}/delete")
def alerts_delete(rule_id: int):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # A firing-history row is meaningless once its rule is gone --
            # alert_log.rule_id FKs to alert_rules, so it has to go first.
            cur.execute("DELETE FROM alert_log WHERE rule_id=%s", (rule_id,))
            cur.execute("DELETE FROM alert_rules WHERE id=%s", (rule_id,))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/alerts", status_code=303)


@app.get("/import", response_class=HTMLResponse)
def import_form(request: Request):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_sources")
            sources = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "import.html", {"sources": sources, "result": None})


@app.post("/import", response_class=HTMLResponse)
def import_run(
    request: Request, path: str = Form(...), game: str = Form("auto"),
    character: str = Form(""), server: str = Form(""), live: bool = Form(False),
):
    import subprocess

    cmd = [sys.executable, "-m", "eq_log_suite.importer", path, "--game", game]
    if character:
        cmd += ["--character", character]
    if server:
        cmd += ["--server", server]
    if live:
        cmd.append("--live")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_sources")
            sources = cur.fetchall()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "import.html", {
        "sources": sources,
        "result": {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode},
    })


@app.post("/import/rescan", response_class=HTMLResponse)
def import_rescan(request: Request):
    import os
    import signal

    from eq_log_suite import discovery
    from eq_log_suite.tailer import PID_PATH

    log_roots = db.config().get("log_roots", {})
    new_sources = discovery.scan_and_import(
        log_roots.get("eql", ""), log_roots.get("eq", "")
    )

    tailer_notified = False
    tailer_error = None
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGUSR1)
        tailer_notified = True
    except FileNotFoundError:
        tailer_error = "Tailer isn't running (no PID file) -- start it before rescanning to actually live-tail anything new."
    except (ValueError, ProcessLookupError):
        tailer_error = "Tailer's PID file is stale (process not running) -- restart the tailer."

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM log_sources")
            sources = cur.fetchall()
    finally:
        conn.close()

    if new_sources:
        message = f"Found and imported {len(new_sources)} new file(s): " + ", ".join(s["file_path"] for s in new_sources)
    else:
        message = "No new files found."
    if tailer_notified:
        message += " Tailer notified -- it'll start live-tailing anything new within a couple seconds."
    elif tailer_error:
        message += f" {tailer_error}"

    return templates.TemplateResponse(request, "import.html", {
        "sources": sources,
        "result": {"stdout": message, "stderr": "", "returncode": 0},
    })
