import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from eq_log_suite import db

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent

app = FastAPI(title="EQ Log Suite")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Zone correlation for an event `ev`: the most recent zone_change logged
# before it. Falls back to zone_start_overrides (a one-time, per-character,
# user-confirmed answer -- see schema.sql) for the leading gap before a
# character's first-ever logged zone_change, where there's nothing to
# correlate against. Used identically by /loot, /gathering, and /quests.
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


@app.get("/eq2", response_class=HTMLResponse)
def eq2_hub(request: Request):
    return templates.TemplateResponse(request, "game_hub.html", {
        "game": "eq2", "game_label": "EverQuest II",
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


def _compute_gathering(game, character="", zone="", node="", item="", skill="", era="current"):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM gather_eras ORDER BY started_at DESC")
            eras = cur.fetchall()
    finally:
        conn.close()

    clauses, params = ["ev.event_type = 'gather'", "g.code = %s"], [game]
    if character:
        clauses.append("c.name = %s"); params.append(character)
    if node:
        clauses.append("JSON_UNQUOTE(JSON_EXTRACT(ev.extra, '$.node')) LIKE %s"); params.append(f"%{node}%")
    if item:
        clauses.append("ev.target_name LIKE %s"); params.append(f"%{item}%")
    if skill:
        clauses.append("ev.verb = %s"); params.append(skill)

    # "current" (default) = the active era (ended_at IS NULL); "all" = no
    # boundary at all; anything else = a specific past era's [start, end).
    selected_era = None
    if era == "current":
        selected_era = next((e for e in eras if e["ended_at"] is None), None)
    elif era != "all":
        selected_era = next((e for e in eras if str(e["id"]) == era), None)
    if selected_era:
        clauses.append("ev.ts >= %s"); params.append(selected_era["started_at"])
        if selected_era["ended_at"]:
            clauses.append("ev.ts < %s"); params.append(selected_era["ended_at"])

    where = f"WHERE {' AND '.join(clauses)}"
    zone_having = "HAVING zone LIKE %s" if zone else ""
    zone_param = [f"%{zone}%"] if zone else []
    sql = (
        # No explicit "tier" exists in the log text -- node_tiers is a
        # user-curated node -> tier mapping (see schema.sql), and any node
        # missing from it shows up flagged so nothing goes unnoticed, for
        # any node past or future. zone is a secondary hint (the zone you
        # were in at the moment of each pull, same technique as /zones),
        # useful context while you're identifying a new node's tier.
        # Gather yields also come from a small set of fixed quantity tiers
        # per item, not a continuous range -- grouping by amount too
        # (instead of averaging it away) surfaces each tier as its own
        # distinct outcome with its own real chance, e.g. "2x tuber strand"
        # and "7x tuber strand" as separate rows rather than one row
        # averaging to 3.9.
        "WITH per_event AS ("
        "  SELECT JSON_UNQUOTE(JSON_EXTRACT(ev.extra, '$.node')) AS node, "
        f"    {_ZONE_LOOKUP_EXPR} AS zone, "
        "    ev.target_name AS item, ev.amount AS qty, ev.verb AS skill, ev.outcome AS outcome "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        f"  {where}"
        "), grouped AS ("
        "  SELECT node, MAX(zone) AS zone, item, qty, skill, "
        "    COUNT(*) AS pulls, SUM(outcome = 'rare') AS rare_pulls "
        "  FROM per_event GROUP BY node, item, qty, skill"
        ") "
        "SELECT grp.node, nt.tier, grp.zone, grp.item, grp.qty, grp.skill, grp.pulls, grp.rare_pulls, "
        "ROUND(grp.pulls / SUM(grp.pulls) OVER (PARTITION BY grp.node) * 100, 2) AS chance_pct "
        "FROM grouped grp "
        "LEFT JOIN node_tiers nt ON nt.node = grp.node "
        f"{zone_having} "
        "ORDER BY (nt.tier IS NULL) DESC, grp.node, chance_pct DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params + zone_param)
            rows = cur.fetchall()
            cur.execute("SELECT DISTINCT verb FROM events ev JOIN games g ON ev.game_id=g.id "
                        "WHERE ev.event_type='gather' AND g.code = %s ORDER BY verb", (game,))
            skills = [r["verb"] for r in cur.fetchall()]
    finally:
        conn.close()

    unidentified_nodes = sorted({r["node"] for r in rows if r["tier"] is None})

    return {
        "rows": rows,
        "skills": skills,
        "unidentified_nodes": unidentified_nodes,
        "eras": eras,
        "selected_era": era,
        "filters": {"character": character, "zone": zone, "node": node, "item": item, "skill": skill},
    }


@app.get("/gathering/eql", response_class=HTMLResponse)
def gathering_report_eql(
    request: Request, character: str = "", zone: str = "", node: str = "",
    item: str = "", skill: str = "", era: str = "current",
):
    ctx = _compute_gathering("eql", character, zone, node, item, skill, era)
    ctx.update({"game": "eql", "game_label": "EQ Legends", "unresolved_zone_starts": _unresolved_zone_starts("eql")})
    return templates.TemplateResponse(request, "gathering.html", ctx)


@app.get("/gathering/eq2", response_class=HTMLResponse)
def gathering_report_eq2(
    request: Request, character: str = "", zone: str = "", node: str = "",
    item: str = "", skill: str = "", era: str = "current",
):
    ctx = _compute_gathering("eq2", character, zone, node, item, skill, era)
    ctx.update({"game": "eq2", "game_label": "EverQuest II", "unresolved_zone_starts": _unresolved_zone_starts("eq2")})
    return templates.TemplateResponse(request, "gathering.html", ctx)


@app.post("/gathering/set-tier")
def gathering_set_tier(node: str = Form(...), tier: str = Form(...), note: str = Form(""), game: str = Form("eql")):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO node_tiers (node, tier, note) VALUES (%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE tier=VALUES(tier), note=VALUES(note)",
                (node, tier, note or None),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/gathering/{game}", status_code=303)


@app.post("/gathering/new-era")
def gathering_new_era(name: str = Form(...), note: str = Form(""), game: str = Form("eql")):
    from eq_log_suite.gather_eras import new_era

    new_era(name, note or None, at=None)
    return RedirectResponse(f"/gathering/{game}", status_code=303)


def _compute_loot(game, npc="", item="", zone=""):
    having_clauses, having_params = [], []
    if zone:
        having_clauses.append("zone LIKE %s"); having_params.append(f"%{zone}%")
    if npc:
        having_clauses.append("npc LIKE %s"); having_params.append(f"%{npc}%")
    if item:
        having_clauses.append("item LIKE %s"); having_params.append(f"%{item}%")
    having = f"HAVING {' AND '.join(having_clauses)}" if having_clauses else ""

    sql = (
        # Drop chance needs kills as the denominator -- not every kill drops
        # anything, so "how often does X drop" only means something relative
        # to "how many times did I kill it". Unlike gathering, loot quantity
        # isn't a small set of fixed tiers -- just average it per (zone, npc, item).
        # zone isn't stated on the loot/death line itself -- correlate against
        # the most recent zone_change for that character, same technique
        # /gathering and /quests already use. Zone is a real grouping level
        # here (not just a hint) since the page is organized zone -> npc ->
        # item, so kills are correlated to zone too (a null-safe join, <=>,
        # since a kill/drop before any zone_change is logged has zone=NULL).
        "WITH kills AS ("
        "  SELECT ev.target_name AS npc, "
        f"         {_ZONE_LOOKUP_EXPR} AS zone, "
        "         COUNT(*) AS kill_count "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        "  WHERE ev.event_type='death' AND ev.source_type='you' AND g.code = %s "
        "  GROUP BY ev.target_name, zone"
        "), loot_events AS ("
        "  SELECT ev.source_name AS npc, ev.target_name AS item, ev.amount AS qty, "
        f"         {_ZONE_LOOKUP_EXPR} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        "  WHERE ev.event_type='loot' AND g.code = %s"
        "), loot_grouped AS ("
        "  SELECT npc, zone, item, ROUND(AVG(qty), 1) AS avg_qty, COUNT(*) AS drops "
        "  FROM loot_events GROUP BY npc, zone, item"
        ") "
        "SELECT lg.zone, lg.npc, lg.item, lg.avg_qty, lg.drops, k.kill_count, "
        "ROUND(lg.drops / k.kill_count * 100, 2) AS chance_pct "
        "FROM loot_grouped lg LEFT JOIN kills k ON k.npc = lg.npc AND k.zone <=> lg.zone "
        f"{having} ORDER BY lg.zone, lg.npc, chance_pct DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (game, game) + tuple(having_params))
            rows = cur.fetchall()
    finally:
        conn.close()

    # groupby() in the template re-sorts by zone then npc -- None (a kill/drop
    # before any zone_change was ever logged) can't be ordered against itself,
    # so normalize to a sortable placeholder here rather than at render time.
    for r in rows:
        r["zone"] = r["zone"] or "(unknown zone)"
        r["npc"] = r["npc"] or "(unknown npc)"
    return rows


@app.get("/loot/eql", response_class=HTMLResponse)
def loot_report_eql(request: Request, npc: str = "", item: str = "", zone: str = ""):
    rows = _compute_loot("eql", npc, item, zone)
    return templates.TemplateResponse(request, "loot.html", {
        "rows": rows,
        "game": "eql",
        "game_label": "EQ Legends",
        "unresolved_zone_starts": _unresolved_zone_starts("eql"),
        "filters": {"npc": npc, "item": item, "zone": zone},
    })


@app.get("/loot/eq2", response_class=HTMLResponse)
def loot_report_eq2(request: Request, npc: str = "", item: str = "", zone: str = ""):
    rows = _compute_loot("eq2", npc, item, zone)
    return templates.TemplateResponse(request, "loot.html", {
        "rows": rows,
        "game": "eq2",
        "game_label": "EverQuest II",
        "unresolved_zone_starts": _unresolved_zone_starts("eq2"),
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
        # recently before it for that character (same technique /gathering
        # and /zones already use). npc is a best-effort "who was I probably
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


@app.get("/quests/eql", response_class=HTMLResponse)
def quests_report_eql(request: Request, character: str = "", quest: str = "", zone: str = "", npc: str = ""):
    rows = _compute_quests("eql", character, quest, zone, npc)
    return templates.TemplateResponse(request, "quests.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
        "unresolved_zone_starts": _unresolved_zone_starts("eql"),
        "filters": {"character": character, "quest": quest, "zone": zone, "npc": npc},
    })


@app.get("/quests/eq2", response_class=HTMLResponse)
def quests_report_eq2(request: Request, character: str = "", quest: str = "", zone: str = "", npc: str = ""):
    rows = _compute_quests("eq2", character, quest, zone, npc)
    return templates.TemplateResponse(request, "quests.html", {
        "rows": rows, "game": "eq2", "game_label": "EverQuest II",
        "unresolved_zone_starts": _unresolved_zone_starts("eq2"),
        "filters": {"character": character, "quest": quest, "zone": zone, "npc": npc},
    })


def _compute_attack_breakdown(conn, direction, character="", game="", start_ts="", end_ts=""):
    """Per-verb (melee attack type or spell name) combat breakdown: hit/crit
    rate, evasion (dodge/parry/block/riposte) rate, resist rate (outgoing
    only -- no sample of an incoming "you resisted X's spell" line exists
    yet to build that pattern from), and min/avg/max damage split by hit vs
    crit. direction='out' is your own attacks (source_type='you'), 'in' is
    attacks against you (target_type='you')."""
    clauses, params = [], []
    if direction == "out":
        clauses.append("e.source_type='you'")
        clauses.append("e.event_type IN ('melee','spell_damage','spell_resist')")
    else:
        clauses.append("e.target_type='you'")
        clauses.append("e.event_type IN ('melee','spell_damage')")
    clauses.append("e.verb IS NOT NULL")
    if character:
        clauses.append("c.name = %s"); params.append(character)
    if game:
        clauses.append("g.code = %s"); params.append(game)
    if start_ts:
        clauses.append("e.ts >= %s"); params.append(start_ts)
    if end_ts:
        clauses.append("e.ts <= %s"); params.append(end_ts)
    where = " AND ".join(clauses)

    sql = (
        "SELECT e.verb, "
        "  COUNT(*) AS attempts, "
        "  SUM(e.outcome IN ('hit','crit')) AS landed, "
        "  SUM(e.outcome='crit') AS crits, "
        "  SUM(e.outcome='miss') AS misses, "
        "  SUM(e.outcome='dodge') AS dodged, "
        "  SUM(e.outcome='parry') AS parried, "
        "  SUM(e.outcome='block') AS blocked, "
        "  SUM(e.outcome='riposte') AS riposted, "
        "  SUM(e.outcome='resist') AS resisted, "
        "  MIN(CASE WHEN e.outcome='hit' THEN e.amount END) AS min_hit, "
        "  AVG(CASE WHEN e.outcome='hit' THEN e.amount END) AS avg_hit, "
        "  MAX(CASE WHEN e.outcome='hit' THEN e.amount END) AS max_hit, "
        "  MIN(CASE WHEN e.outcome='crit' THEN e.amount END) AS min_crit, "
        "  AVG(CASE WHEN e.outcome='crit' THEN e.amount END) AS avg_crit, "
        "  MAX(CASE WHEN e.outcome='crit' THEN e.amount END) AS max_crit "
        "FROM events e JOIN characters c ON e.character_id=c.id JOIN games g ON g.id=c.game_id "
        f"WHERE {where} "
        "GROUP BY e.verb ORDER BY attempts DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    def pct(num, den):
        return round(num / den * 100, 1) if den else None

    result = []
    for r in rows:
        attempts = int(r["attempts"])
        landed = int(r["landed"])
        result.append({
            "verb": r["verb"],
            "attempts": attempts,
            "hit_pct": pct(landed, attempts),
            "crit_pct": pct(int(r["crits"]), landed),
            "miss_pct": pct(int(r["misses"]), attempts),
            "dodge_pct": pct(int(r["dodged"]), attempts),
            "parry_pct": pct(int(r["parried"]), attempts),
            "block_pct": pct(int(r["blocked"]), attempts),
            "riposte_pct": pct(int(r["riposted"]), attempts),
            "resist_pct": pct(int(r["resisted"]), attempts),
            "min_hit": r["min_hit"],
            "avg_hit": round(float(r["avg_hit"]), 1) if r["avg_hit"] is not None else None,
            "max_hit": r["max_hit"],
            "min_crit": r["min_crit"],
            "avg_crit": round(float(r["avg_crit"]), 1) if r["avg_crit"] is not None else None,
            "max_crit": r["max_crit"],
        })
    return result


def _render_attacks(request, game, game_label, character, start_ts, end_ts):
    conn = db.get_connection()
    try:
        outgoing = _compute_attack_breakdown(conn, "out", character, game, start_ts, end_ts)
        incoming = _compute_attack_breakdown(conn, "in", character, game, start_ts, end_ts)
    finally:
        conn.close()
    return templates.TemplateResponse(request, "attacks.html", {
        "outgoing": outgoing,
        "incoming": incoming,
        "game": game,
        "game_label": game_label,
        "filters": {"character": character, "start_ts": start_ts, "end_ts": end_ts},
    })


@app.get("/attacks/eql", response_class=HTMLResponse)
def attacks_breakdown_eql(request: Request, character: str = "", start_ts: str = "", end_ts: str = ""):
    return _render_attacks(request, "eql", "EQ Legends", character, start_ts, end_ts)


@app.get("/attacks/eq2", response_class=HTMLResponse)
def attacks_breakdown_eq2(request: Request, character: str = "", start_ts: str = "", end_ts: str = ""):
    return _render_attacks(request, "eq2", "EverQuest II", character, start_ts, end_ts)


def _compute_zones(game, character=""):
    char_clauses, char_params = ["g.code = %s"], [game]
    if character:
        char_clauses.append("c.name = %s"); char_params.append(character)
    char_where = f"AND {' AND '.join(char_clauses)}" if char_clauses else ""

    # Zone "sessions" are the gap between one zone_change and the character's
    # next one; combat stats for that session are whatever damage/kill/melee
    # events from you fall inside that time window. A NULL left_at means
    # you're still in that zone (the most recent zone_change with nothing after it).
    sql = (
        "WITH zone_sessions AS ("
        "  SELECT e.character_id, e.target_name AS zone, e.ts AS entered_at, "
        "         LEAD(e.ts) OVER (PARTITION BY e.character_id ORDER BY e.ts) AS left_at "
        "  FROM events e JOIN games g ON e.game_id=g.id JOIN characters c ON e.character_id=c.id "
        f"  WHERE e.event_type = 'zone_change' {char_where}"
        ") "
        "SELECT c.name AS character_name, g.code AS game, zs.zone, zs.entered_at, zs.left_at, "
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
        "GROUP BY zs.character_id, zs.zone, zs.entered_at, zs.left_at "
        "ORDER BY zs.entered_at DESC"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, char_params)
            rows = cur.fetchall()
    finally:
        conn.close()

    for r in rows:
        total_damage = float(r["total_damage"] or 0)
        r["total_damage"] = total_damage
        if r["entered_at"] and r["left_at"]:
            r["duration_s"] = round((r["left_at"] - r["entered_at"]).total_seconds())
        else:
            r["duration_s"] = None
        if r["first_fight_at"] and r["last_fight_at"]:
            span = (r["last_fight_at"] - r["first_fight_at"]).total_seconds()
            r["dps"] = round(total_damage / span, 1) if span > 0 else total_damage
        else:
            r["dps"] = None
        r["hit_pct"] = round(r["landed"] / r["swings"] * 100, 1) if r["swings"] else None
        r["crit_pct"] = round(r["crits"] / r["landed"] * 100, 1) if r["landed"] else None

    return rows


@app.get("/zones/eql", response_class=HTMLResponse)
def zones_report_eql(request: Request, character: str = ""):
    rows = _compute_zones("eql", character)
    return templates.TemplateResponse(request, "zones.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
        "filters": {"character": character},
    })


@app.get("/zones/eq2", response_class=HTMLResponse)
def zones_report_eq2(request: Request, character: str = ""):
    rows = _compute_zones("eq2", character)
    return templates.TemplateResponse(request, "zones.html", {
        "rows": rows, "game": "eq2", "game_label": "EverQuest II",
        "filters": {"character": character},
    })


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
# EQ2 toggles "You stop fighting."/"You start fighting." between individual
# mobs in the same pull (observed: as little as 3s apart for back-to-back
# kills), which reads as one continuous fight to the player but as several
# separate combat_state pairs in the log. Merge consecutive pairs into one
# encounter when the gap between a stop and the next start is small --
# matches the same "still the same fight" convention as COMBAT_TIMEOUT_SECONDS
# in tailer.py's live DPS tracker.
ENCOUNTER_MERGE_GAP_SECONDS = 6


def _merge_encounters(marker_rows):
    """marker_rows: combat_state rows (character_id, character_name, game,
    ts, verb), ordered by ts, for potentially several characters interleaved.
    Returns merged (character_id, character_name, game, start_ts, stop_ts)
    tuples, stop_ts=None meaning still active."""
    open_encounters: dict[int, dict] = {}  # character_id -> in-progress encounter
    finished = []

    for row in marker_rows:
        cid = row["character_id"]
        current = open_encounters.get(cid)
        if row["verb"] == "start":
            if current is None:
                open_encounters[cid] = {
                    "character_id": cid, "character_name": row["character_name"],
                    "game": row["game"], "start_ts": row["ts"], "stop_ts": None,
                }
            elif current["stop_ts"] is not None:
                gap = (row["ts"] - current["stop_ts"]).total_seconds()
                if gap <= ENCOUNTER_MERGE_GAP_SECONDS:
                    current["stop_ts"] = None  # merge: still the same encounter
                else:
                    finished.append(current)
                    open_encounters[cid] = {
                        "character_id": cid, "character_name": row["character_name"],
                        "game": row["game"], "start_ts": row["ts"], "stop_ts": None,
                    }
            # else: a 'start' while already active (no intervening stop) -- ignore, already open
        elif row["verb"] == "stop" and current is not None and current["stop_ts"] is None:
            current["stop_ts"] = row["ts"]

    for current in open_encounters.values():
        finished.append(current)  # includes any still-active (stop_ts=None) encounter
    finished.sort(key=lambda e: e["start_ts"], reverse=True)
    return finished


# EQL has no "You stop fighting." marker at all (unlike EQ2), so its
# encounters have to be derived purely from activity: out of combat is 5
# seconds since the last combat action, same idea as the DPS meter's own
# pause timeout (COMBAT_TIMEOUT_SECONDS in tailer.py), just EQL-specific gap
# length -- OR immediately, regardless of the timer, on your own death,
# using Escape, or a zone change (hard stops). Games with an explicit marker
# (EQ2) use _merge_encounters instead, which is more precise when available.
EQL_OUT_OF_COMBAT_SECONDS = 5


def _derive_gap_based_encounters(rows, gap_seconds):
    """rows: chronological (character_id, character_name, game, ts,
    is_hard_stop) rows -- regular combat activity (is_hard_stop=False)
    extends the encounter and is closed after gap_seconds of no further
    activity; a hard-stop row (your death, Escape, zone change) closes it
    immediately at its own timestamp instead, without itself starting a new
    encounter."""
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

        if row["is_hard_stop"]:
            if current is not None:
                current["stop_ts"] = row["ts"]
                finished.append(current)
                open_encounters[cid] = None
            continue

        if current is None:
            current = {
                "character_id": cid, "character_name": row["character_name"],
                "game": row["game"], "start_ts": row["ts"], "last_ts": row["ts"], "stop_ts": None,
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


def _compute_encounters(character="", start_ts="", end_ts="", limit=30, ascending=False):
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
            cur.execute(
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, ev.verb "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                f"WHERE ev.event_type='combat_state' {where} ORDER BY ev.ts",
                params,
            )
            markers = cur.fetchall()

            # Regular activity: extends the encounter, subject to the
            # gap_seconds timeout. Damage either direction, or a kill BY you
            # (an NPC's death doesn't end your combat -- there may be adds).
            cur.execute(
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, 0 AS is_hard_stop "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                "WHERE g.code != 'eq2' AND ("
                f"  (ev.event_type IN {DAMAGE_EVENT_TYPES_SQL} AND (ev.source_type='you' OR ev.target_type='you'))"
                "  OR (ev.event_type='death' AND ev.source_type='you')"
                f") {where} "
                "UNION ALL "
                # Hard stops: close the encounter immediately regardless of
                # the timer -- your own death, Escape, or a zone change.
                "SELECT ev.character_id, c.name AS character_name, g.code AS game, ev.ts, 1 AS is_hard_stop "
                "FROM events ev JOIN characters c ON ev.character_id=c.id JOIN games g ON g.id=c.game_id "
                "WHERE g.code != 'eq2' AND ("
                "  (ev.event_type='death' AND ev.target_type='you')"
                "  OR ev.event_type='escape'"
                "  OR (ev.event_type='zone_change' AND ev.source_type='you')"
                f") {where} "
                "ORDER BY ts",
                params + params,
            )
            activity = cur.fetchall()

        encounters = _merge_encounters(markers) + _derive_gap_based_encounters(activity, EQL_OUT_OF_COMBAT_SECONDS)
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
            swings = int(s["swings"] or 0)
            landed = int(s["landed"] or 0)
            crits = int(s["crits"] or 0)
            kills = int(s["kills"] or 0)
            result.append({
                "character": enc["character_name"],
                "game": enc["game"],
                "start_ts": enc["start_ts"].isoformat(),
                "stop_ts": end.isoformat() if end else None,
                "active": active,
                "duration_s": round(span),
                "damage_out": damage_out,
                "damage_in": damage_in,
                "dps_out": round(damage_out / span, 1),
                "dps_in": round(damage_in / span, 1),
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
def api_encounters(character: str = "", start_ts: str = "", end_ts: str = "", limit: int = 30):
    return JSONResponse(_compute_encounters(character, start_ts, end_ts, limit))


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
    request: Request, character: str = "", start_ts: str = "", end_ts: str = "",
    limit: int = 200, order: str = "desc",
):
    limit = max(0, min(limit, 5000))
    # Defaults to showing nothing until a log + date range is picked -- an
    # unbounded fetch here means pulling every combat event in the DB into
    # Python before merging (already ~240k rows after under a month of
    # play), so "run automatically on page load" doesn't scale.
    ready = bool(character and start_ts and end_ts)
    encounters = []
    totals = {"count": 0, "damage_out": 0, "damage_in": 0, "kills": 0, "duration_s": 0}
    if ready:
        encounters = _compute_encounters(character, start_ts, end_ts, limit, ascending=(order == "asc"))
        totals = {
            "count": len(encounters),
            "damage_out": sum(e["damage_out"] for e in encounters),
            "damage_in": sum(e["damage_in"] for e in encounters),
            "kills": sum(e["kills"] for e in encounters),
            "duration_s": sum(e["duration_s"] for e in encounters),
        }

    return templates.TemplateResponse(request, "encounters.html", {
        "encounters": encounters,
        "totals": totals,
        "ready": ready,
        "known_logs": _known_logs(),
        "filters": {"character": character, "start_ts": start_ts, "end_ts": end_ts, "limit": limit, "order": order},
    })


def _compute_encounter_combat_breakdown(character, start_ts, stop_ts):
    """Full per-encounter breakdown: every attack type (melee verb or spell
    name), split by combatant vs NPC and by direction (combatant attacking
    the NPC, or the NPC attacking the combatant) -- the /attacks page's
    per-verb granularity, scoped to one encounter and not just to 'you'.
    Same two-pass mob-vs-combatant resolution as _compute_encounters/
    api_encounter_detail: confirm mobs via your own engagement first (since
    a proper-named boss and a player name are textually identical), then use
    that plus unambiguous article-prefixed NPC names to sort everyone else's
    lines into combatant vs NPC too. EQL is combat-locked to your own
    group/raid, so anyone showing up this way is legitimately part of it.

    Returns (verb_rows, npc_totals): verb_rows is the flat per-(combatant,
    npc, verb, direction) list; npc_totals is per-NPC melee/spell damage
    aggregated across every combatant, keyed by NPC name."""
    clauses = ["c.name = %s", "ev.ts >= %s"]
    params = [character, start_ts]
    if stop_ts:
        clauses.append("ev.ts <= %s")
        params.append(stop_ts)

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ev.source_name, ev.source_type, ev.target_name, ev.target_type, "
                "ev.event_type, ev.verb, ev.amount, ev.outcome "
                "FROM events ev JOIN characters c ON ev.character_id=c.id "
                f"WHERE {' AND '.join(clauses)} "
                "AND ev.event_type IN ('melee','spell_damage','ability_damage','spell_resist') "
                "AND ev.verb IS NOT NULL",
                params,
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    confirmed_mobs = set()
    for ev in rows:
        if ev["source_type"] == "you" and ev["target_name"]:
            confirmed_mobs.add(ev["target_name"])
        elif ev["target_type"] == "you" and ev["source_name"]:
            confirmed_mobs.add(ev["source_name"])

    def is_mob(actor_type, name):
        return bool(name) and (actor_type == "npc" or name in confirmed_mobs)

    buckets: dict[tuple, dict] = {}

    def bucket(combatant, npc, verb, direction):
        key = (combatant, npc, verb, direction)
        return buckets.setdefault(key, {
            "combatant": combatant, "npc": npc, "verb": verb, "direction": direction,
            "attempts": 0, "landed": 0, "crits": 0, "misses": 0,
            "dodged": 0, "parried": 0, "blocked": 0, "riposted": 0, "resisted": 0,
            "hit_amounts": [], "crit_amounts": [],
        })

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
            combatant = "You" if s_type == "you" else s_name
            npc, direction = t_name, "out"
            nt = npc_total(npc)
            nt["melee_to" if is_melee else "spell_to"] += amount
        elif s_is_mob and not t_is_mob:
            combatant = "You" if t_type == "you" else t_name
            npc, direction = s_name, "in"
            nt = npc_total(npc)
            nt["melee_from" if is_melee else "spell_from"] += amount
        else:
            continue  # both or neither side reads as a mob -- too ambiguous to place

        b = bucket(combatant, npc, ev["verb"], direction)
        b["attempts"] += 1
        outcome = ev["outcome"]
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

    def pct(n, den):
        return round(n / den * 100, 1) if den else None

    verb_rows = []
    for b in buckets.values():
        attempts, landed = b["attempts"], b["landed"]
        hit_amts, crit_amts = b["hit_amounts"], b["crit_amounts"]
        verb_rows.append({
            "combatant": b["combatant"], "npc": b["npc"], "verb": b["verb"], "direction": b["direction"],
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
            "min_hit": min(hit_amts) if hit_amts else None,
            "avg_hit": round(sum(hit_amts) / len(hit_amts), 1) if hit_amts else None,
            "max_hit": max(hit_amts) if hit_amts else None,
            "min_crit": min(crit_amts) if crit_amts else None,
            "avg_crit": round(sum(crit_amts) / len(crit_amts), 1) if crit_amts else None,
            "max_crit": max(crit_amts) if crit_amts else None,
        })
    verb_rows.sort(key=lambda r: (r["npc"], r["combatant"], r["direction"], -r["attempts"]))
    return verb_rows, npc_totals


@app.get("/encounters/breakdown", response_class=HTMLResponse)
def encounter_breakdown(request: Request, character: str, start_ts: str, stop_ts: str = ""):
    verb_rows, npc_totals = _compute_encounter_combat_breakdown(character, start_ts, stop_ts)

    start_dt = datetime.fromisoformat(start_ts)
    end_dt = datetime.fromisoformat(stop_ts) if stop_ts else datetime.now()
    duration_s = max(round((end_dt - start_dt).total_seconds()), 1)

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
        you_dirs = combatants.pop("You", {"out": [], "in": []})
        others = sorted(
            (combatant_summary(name, dirs) for name, dirs in combatants.items()),
            key=lambda c: -c["out_total"],
        )
        nt = npc_totals.get(npc_name, {"melee_to": 0, "spell_to": 0, "melee_from": 0, "spell_from": 0})
        npcs.append({
            "npc": npc_name,
            "melee_damage_to": nt["melee_to"], "spell_damage_to": nt["spell_to"],
            "melee_damage_from": nt["melee_from"], "spell_damage_from": nt["spell_from"],
            "you": combatant_summary("You", you_dirs),
            "others": others,
        })
    npcs.sort(key=lambda n: -(n["melee_damage_to"] + n["spell_damage_to"]))

    return templates.TemplateResponse(request, "encounter_breakdown.html", {
        "npcs": npcs,
        "start_ts": start_ts,
        "stop_ts": stop_ts or end_dt.isoformat(timespec="seconds"),
        "duration_s": duration_s,
        "filters": {"character": character, "start_ts": start_ts, "stop_ts": stop_ts},
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


@app.post("/alerts/create")
def alerts_create(
    name: str = Form(...), description: str = Form(""), game_id: str = Form(""),
    match_type: str = Form(...), pattern: str = Form(...),
    reaction_types: list[str] = Form([]), sound_file: str = Form(""),
    overlay_text: str = Form(""), cooldown_seconds: int = Form(0),
):
    reaction_config = {}
    if sound_file:
        reaction_config["sound_file"] = sound_file
    if overlay_text:
        reaction_config["overlay_text"] = overlay_text

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
    new_sources = discovery.scan_and_import(log_roots.get("eql", ""), log_roots.get("eq2", ""))

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
