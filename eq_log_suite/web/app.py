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

# Serves the raw screenshots behind /items/review so the review page can show
# the source image next to the OCR text -- same folder item_capture_watcher
# polls (config/local.yaml: item_capture.screenshot_dir).
_ITEM_SCREENSHOT_DIR = db.config().get("item_capture", {}).get("screenshot_dir")
if _ITEM_SCREENSHOT_DIR and Path(_ITEM_SCREENSHOT_DIR).is_dir():
    app.mount("/item-screenshots", StaticFiles(directory=_ITEM_SCREENSHOT_DIR), name="item_screenshots")


def _item_screenshot_url(path: str | None) -> str | None:
    if not path or not _ITEM_SCREENSHOT_DIR:
        return None
    return f"/item-screenshots/{Path(path).name}"


def _parse_stats_json(raw: str | None) -> dict:
    # pymysql doesn't auto-deserialize JSON columns -- item_info.stats comes
    # back as a raw JSON string, parsed here so templates can render it as a
    # table instead of a blob.
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}

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


@app.get("/gathering/eq2", response_class=HTMLResponse)
def gathering_report_eq2(
    request: Request, character: str = "", zone: str = "", node: str = "",
    item: str = "", skill: str = "", era: str = "current",
):
    ctx = _compute_gathering("eq2", character, zone, node, item, skill, era)
    ctx.update({"game": "eq2", "game_label": "EverQuest II", "unresolved_zone_starts": _unresolved_zone_starts("eq2")})
    return templates.TemplateResponse(request, "gathering.html", ctx)


@app.post("/gathering/set-tier")
def gathering_set_tier(node: str = Form(...), tier: str = Form(...), note: str = Form(""), game: str = Form("eq2")):
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
def gathering_new_era(name: str = Form(...), note: str = Form(""), game: str = Form("eq2")):
    from eq_log_suite.gather_eras import new_era

    new_era(name, note or None, at=None)
    return RedirectResponse(f"/gathering/{game}", status_code=303)


LOOT_RESULT_LIMIT = 50


def _compute_loot(game, npc="", item="", zone=""):
    # /loot used to render the *entire* zone->npc->item drop table
    # unconditionally (~0.7s of correlated zone lookups over every loot/
    # death event, every single page load) now that /zoneinfo covers
    # per-zone browsing and each NPC's own page covers per-npc browsing --
    # this is search-only now (see the two routes below, which just don't
    # call this at all with no filters given). npc/item filters are pushed
    # into loot_events' WHERE (not a post-aggregation HAVING) so a real
    # search also does meaningfully less correlated zone-lookup work, not
    # just skip the no-filter case: e.g. searching one item name only
    # correlates zone for the handful of loot rows matching that name, not
    # all ~1800. kills is restricted to just the npcs loot_events actually
    # produced, for the same reason -- chance% never needs a kill count for
    # an npc with no matching loot row.
    #
    # Future extension point: once items have a curated type (trash/quest/
    # tradeskill/weapon slot+type/armor slot+type -- not built yet), that'd
    # join in here (e.g. an `item_info` table keyed on item name, same
    # shape as node_tiers/npc_info/zone_info) and surface as a filter/column
    # alongside npc/item/zone.
    loot_clauses, loot_params = ["ev.event_type='loot'", "g.code = %s"], [game]
    if npc:
        loot_clauses.append("ev.source_name LIKE %s"); loot_params.append(f"%{npc}%")
    if item:
        loot_clauses.append("ev.target_name LIKE %s"); loot_params.append(f"%{item}%")

    zone_where = ""
    zone_params = []
    if zone:
        # zone comes from _base_zone_expr(), a JSON_UNQUOTE(JSON_EXTRACT(...))
        # expression -- MariaDB gives those utf8mb4_bin collation (binary,
        # case-sensitive) regardless of the source column's own collation,
        # unlike npc/item above which stay on source_name/target_name's
        # native case-insensitive collation. LOWER() on both sides restores
        # the same case-insensitive matching as every other search field.
        zone_where = "WHERE LOWER(zone) LIKE %s"
        zone_params = [f"%{zone.lower()}%"]

    sql = (
        # Drop chance needs kills as the denominator -- not every kill drops
        # anything, so "how often does X drop" only means something relative
        # to "how many times did I kill it". Unlike gathering, loot quantity
        # isn't a small set of fixed tiers -- just average it per (zone, npc, item).
        # zone isn't stated on the loot/death line itself -- correlate against
        # the most recent zone_change for that character. Uses base_zone
        # (_base_zone_expr, same as /zoneinfo), not the raw tiered string
        # (_ZONE_LOOKUP_EXPR, used by /gathering/EQ2-quests) -- results here
        # link out to /zoneinfo/{game}/detail?zone=..., which is keyed on
        # base zone, so this has to match or the links would 404. Kills are
        # correlated to zone too (a null-safe join, <=>, since a kill/drop
        # before any zone_change is logged has zone=NULL).
        "WITH loot_events AS ("
        "  SELECT ev.source_name AS npc, ev.target_name AS item, ev.amount AS qty, "
        f"         {_base_zone_expr()} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        f"  WHERE {' AND '.join(loot_clauses)}"
        "), loot_grouped AS ("
        "  SELECT npc, zone, item, ROUND(AVG(qty), 1) AS avg_qty, COUNT(*) AS drops "
        "  FROM loot_events GROUP BY npc, zone, item"
        "), kills AS ("
        "  SELECT ev.target_name AS npc, "
        f"         {_base_zone_expr()} AS zone, "
        "         COUNT(*) AS kill_count "
        "  FROM events ev JOIN games g ON ev.game_id=g.id JOIN characters c ON ev.character_id=c.id "
        "  WHERE ev.event_type='death' AND ev.source_type='you' AND g.code = %s "
        "    AND ev.target_name IN (SELECT DISTINCT npc FROM loot_events) "
        "  GROUP BY ev.target_name, zone"
        ") "
        "SELECT * FROM ("
        "  SELECT lg.zone, lg.npc, lg.item, lg.avg_qty, lg.drops, k.kill_count, "
        "  ROUND(lg.drops / k.kill_count * 100, 2) AS chance_pct "
        "  FROM loot_grouped lg LEFT JOIN kills k ON k.npc = lg.npc AND k.zone <=> lg.zone"
        ") t "
        f"{zone_where} ORDER BY item, chance_pct DESC "
        "LIMIT %s"
    )

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # Fetch one row past the limit just to detect truncation, then
            # trim it back off below -- lets a wide-open, no-filter search
            # (npc/item/zone all blank) stay cheap instead of materializing
            # every loot row in the game.
            cur.execute(sql, loot_params + [game] + zone_params + [LOOT_RESULT_LIMIT + 1])
            rows = cur.fetchall()
            truncated = len(rows) > LOOT_RESULT_LIMIT
            rows = rows[:LOOT_RESULT_LIMIT]

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


@app.get("/loot/eq2", response_class=HTMLResponse)
def loot_report_eq2(request: Request, npc: str = "", item: str = "", zone: str = ""):
    rows, truncated = _compute_loot("eq2", npc, item, zone)
    return templates.TemplateResponse(request, "loot.html", {
        "rows": rows,
        "game": "eq2",
        "game_label": "EverQuest II",
        "truncated": truncated,
        "unresolved_zone_starts": _unresolved_zone_starts("eq2"),
        "filters": {"npc": npc, "item": item, "zone": zone},
    })


# Prefill-only guesses for the review form -- confirmed real (screenshots
# dated 2026-08-05): "TierN" (no progress -- standalone Exaltation
# augmentation objects never show progress), "TierN X/Y" (progress toward
# next tier, e.g. "Tier2 0/4", "Tier5 11/32"), or "TierN / N" at the final
# tier (e.g. "Tier 10 / 10" -- confirmed 10 is the current max tier, no
# higher tier exists or is planned, so this isn't "progress toward 11", it's
# a maxed-out indicator). Only equippable items have a Tier at all -- a
# block with none isn't a missed guess, the item just doesn't have the
# concept. Tried most-specific pattern first. Never trusted blind -- the
# human still edits/confirms every field, this just saves retyping the
# common case.
_TIER_PROGRESS_RE = re.compile(r"Tier\s*(\d+)\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_TIER_MAXED_RE = re.compile(r"Tier\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
_TIER_ONLY_RE = re.compile(r"Tier\s*(\d+)", re.IGNORECASE)


def _guess_tier_fields(block_text: str) -> dict:
    if m := _TIER_PROGRESS_RE.search(block_text):
        tier, tier_progress = m.group(1), f"{m.group(2)}/{m.group(3)}"
    elif m := _TIER_MAXED_RE.search(block_text):
        tier, tier_progress = m.group(1), f"{m.group(1)}/{m.group(2)}"
    elif m := _TIER_ONLY_RE.search(block_text):
        tier, tier_progress = m.group(1), ""
    else:
        tier, tier_progress = "", ""
    text = block_text.lower()
    if "cannot be upgraded" in text:
        upgradeable = "no"
    elif "can be upgraded" in text:
        upgradeable = "yes"
    else:
        upgradeable = ""
    return {"tier": tier, "tier_progress": tier_progress, "upgradeable": upgradeable}


# Confirmed real: every item window's tooltip has a "Class: ..." line
# immediately followed by "Race: ..." -- unlike chat/character-sheet/menu
# text a full-screen capture also picks up, which never contains that pair.
# Used as the anchor to split one capture's raw OCR text (which can and does
# hold multiple overlapping item tooltips at once, also confirmed real) into
# one candidate block per item, instead of a single prefill guess searching
# the whole noisy capture and grabbing whichever item's Tier/stats happen to
# match first -- that's what made the first attempt at this bad.
# Tolerates a short OCR-garbage prefix before "Class:" -- confirmed real
# (capture 2026-08-05, "Iron Ration" item): OCR sometimes merges a stray
# character or two onto the front of the line (e.g. "SS Class: ALL"), which
# a strict line-start match would silently miss and drop that item's block
# entirely (surfaces as "no Class:/Race: found" in the review form -- the
# whole item, not just one field, since block-splitting anchors on this).
# Widened twice since: "_Class:" (2026-08-05, "Lamentation Blade") needed
# dropping \b entirely -- "_" is itself a word character, so \b never
# fires between it and "C" no matter how short the prefix; "*ity Class:"
# (same day, "Small Ornate Chain Mail") needed a 5-char prefix, past the
# old 4-char cap.
_CLASS_LINE_RE = re.compile(r"^\s*.{0,8}Class\s*[:;]", re.IGNORECASE)
# Item window layout is fixed and positional (confirmed real, 2026-08-05):
# line 1 name, line 2 trade/equip flags (No Trade/Lore Equipped/Placeable/
# Quest/Attunable/etc.), line 3 Class, line 4 Race, line 5 slot(s). Used to
# be keyword-matched against the flags line to decide "is this the flags
# line or the name" -- switched to pure position because OCR sometimes
# garbles "No Trade" past recognition ("Mn Trade", "er Trade", "rat Trade"
# all seen on real captures), which made the keyword check silently use the
# garbled flags line itself as the item name.
_LEADING_GARBAGE_RE = re.compile(r"^[a-z]{0,3}[^A-Za-z0-9]{0,3}\s*")
# Used only as a safety net when the name line is missing entirely (OCR
# produced zero text for it, not garbled) -- see the len(above) == 1 case
# below. Not used for the normal name-vs-flags routing, which is purely
# positional (a garbled "No Trade" wouldn't match this and that's fine,
# since position already found the right line in that case).
_POSSESSIONS_HINT_RE = re.compile(
    r"no trade|lore|placeable|augmentation|quest|attunable|heirloom|magic", re.IGNORECASE
)
# Confirmed real ("Small Ornate Chain Mail +2", 2026-08-05): the flags
# line's icon-glyph garbage prefix can run much longer than the short
# _LEADING_GARBAGE_RE tolerance ("4 ee (R) Attunable" -- 4 tokens of noise
# before the real word). Fixed the same way as the class-line anchor: stop
# trying to bound how much garbage precedes the real content and instead
# search for where the real content *starts* -- everything from the first
# recognized flag keyword onward, alternation ordered so "lore equipped"
# wins over the bare "lore" it's a superstring of.
_FLAGS_START_RE = re.compile(
    r"(no trade|lore equipped|lore|placeable|augmentation|quest|attunable|heirloom|magic).*",
    re.IGNORECASE,
)


def _clean_flags_line(raw: str) -> str:
    m = _FLAGS_START_RE.search(raw)
    return m.group(0) if m else _LEADING_GARBAGE_RE.sub("", raw)
# Confirmed real: "tells...guild/general/you" alone missed other noise a
# full-screen capture picks up below the item window -- guild achievement
# announcements and the "Taking a screenshot" system message don't match
# that shape (screenshot 2026-08-05, "Large Brick of Ore": its block ran on
# for 30 lines of chat/achievement noise before hitting the line cap).
# Broadened to catch those too rather than relying on MAX_BLOCK_LINES to
# save it.
_CHAT_LINE_RE = re.compile(
    r"\btells\b|\bachievement\b|^\s*taking\b|welcome to everquest|^\s*channels?[\s:]"
    # Also confirmed real (capture 2026-08-05, "Iron Ration" -- the last
    # item in the capture, so it had nothing but the line cap to stop it):
    # the character stat/inventory panel bleeds in the same way chat does
    # once the item's own block is exhausted.
    r"|bind location|origin location|overall weight|equipped weight|next level|next aa",
    re.IGNORECASE,
)
# Ornamentation/Worn consistently OCR as "Omamentation"/"Wom" (tesseract
# reads "rn" as "m" in this font at this resolution -- confirmed real across
# every capture seen so far) -- matched as recognized aliases here so a slot
# doesn't silently come back empty just because of that misread.
# There are exactly five slots (confirmed real, same five in every capture
# so far) -- extracted into their own fields rather than left as a
# free-text blob so each slot's value can be checked against
# already-confirmed item names for spelling consistency (see
# _KNOWN_ITEM_NAMES/items_review below) -- confirmed real that OCR spells
# the same exaltation name differently across captures (e.g. "Ykesha" /
# "Ykeshia" / "Yiesha" for "Short Sword of the Ykesha (Exaltation)").
_EXALTATION_SLOTS = [
    ("ornamentation", ("Ornamentation", "Omamentation")),
    ("focus_exaltation", ("Focus Exaltation",)),
    ("click_exaltation", ("Click Exaltation",)),
    ("worn_exaltation", ("Worn Exaltation", "Wom Exaltation")),
    ("proc_exaltation", ("Proc Exaltation",)),
]


def _guess_exaltation_slots(block_text: str) -> dict:
    slots = {}
    for key, aliases in _EXALTATION_SLOTS:
        pattern = "|".join(re.escape(a) for a in aliases)
        m = re.search(rf"\b(?:{pattern})\s*:\s*(.*)", block_text, re.IGNORECASE)
        value = m.group(1).strip() if m else ""
        if value.lower() in ("empty", ""):
            value = ""
        slots[key] = value
    return slots


# The "+N" at the end of an item's name is its tier, confirmed real -- they
# should always match (e.g. "Bloodmoon +6" is tier 6). Used both as a
# fallback when OCR misses the Tier line entirely, and as a mismatch check
# against whatever tier OCR *did* read (a strong signal one of the two is a
# misread, since these aren't independent facts -- they're the same number
# shown twice).
_NAME_TIER_SUFFIX_RE = re.compile(r"\+(\d+)\s*(?:\(|$)")

MAX_BLOCK_LINES = 30  # safety cap so a missed boundary can't drag in an entire capture

# A tier-progression capture (2026-08-05: "Small Ornate Chain Coif" ->
# "... +1" -> ...) changes the item's own name string, since the "+N" tier
# suffix is part of it -- "Coif" and "Coif +1" are unrelated item_info.item
# strings even though they're the same base item at different tiers.
# _base_item_name() strips that suffix so /items and /items/detail can
# group all tier variants of one item together instead of treating each
# tier as an unrelated item. Anchored to end-of-string (only a trailing
# "+N" is a tier suffix, confirmed real -- no observed case of trailing
# text after it on the item's own name line, unlike the window-title line
# which can add "(Augmented)" but isn't what gets stored).
_BASE_NAME_STRIP_RE = re.compile(r"\s*\+\d+$")
# Numerator of "X/Y" tier progress, for numeric (not string) sorting --
# confirmed real that progress within a tier can itself change an item's
# stats (not just crossing a tier boundary), so the family dropdown needs
# to offer every individual snapshot, sorted correctly, not just one entry
# per tier.
_TIER_PROGRESS_NUMERATOR_RE = re.compile(r"(\d+)\s*/")


def _base_item_name(name: str) -> str:
    return _BASE_NAME_STRIP_RE.sub("", name).strip()


def _tier_sort_key(row: dict):
    tier = row["tier"] if row["tier"] is not None else 0
    m = _TIER_PROGRESS_NUMERATOR_RE.match(row["tier_progress"] or "")
    progress = int(m.group(1)) if m else 0
    return (tier, progress, row["confirmed_at"])

# Generic "Label: Value" field extraction -- confirmed real (2026-08-05,
# after switching capture font to Courier New/monospace, see
# item_capture_watcher's font note): with a clean single-item screenshot
# and a monospace font, OCR is reliable enough that every stat/save/effect
# line reduces to a small, stable label vocabulary shared across every item
# shape seen (weapons, armor, augmentations, tradeskill items). Classifying
# by label text rather than by column position because tesseract's
# image_to_string() collapses the source image's multi-space column gaps
# down to single spaces, so the visual 3-column layout (stats / saves /
# combat effects) isn't recoverable from whitespace -- but the label names
# alone are enough to sort each value into the right bucket.
_STAT_LABELS = [
    "size", "weight", "ac", "base dmg", "delay", "skill", "dmg bon", "ratio",
    "strength", "stamina", "agility", "dexterity", "wisdom", "intelligence",
    "charisma", "hp", "mana", "range", "backstab dmg", "haste", "end",
    "endurance", "hp regen", "mana regen", "end regen", "endurance regen",
]
# Confirmed real: the Proc Exaltation slot's effect line is labeled
# "Combat Effect" in-game, not "Proc Effect" (which was an unconfirmed
# guess by naming-pattern analogy with Focus/Click -- wrong). Kept in the
# list since it's a harmless unused fallback, but "Combat Effect" is the
# one that actually shows up.
_EFFECT_LABELS = [
    "combat effect", "focus effect", "click effect", "proc effect",
    "cast time", "required level", "cooldown",
]
_EXALT_LABEL_CANON = {
    "ornamentation": "Ornamentation", "omamentation": "Ornamentation",
    "focus exaltation": "Focus Exaltation", "click exaltation": "Click Exaltation",
    "worn exaltation": "Worn Exaltation", "wom exaltation": "Worn Exaltation",
    "proc exaltation": "Proc Exaltation",
}
_META_LABELS = ["class", "race"]
# Fields whose value is really a list, not a sentence -- split on whitespace
# at confirm time (see items_confirm) rather than stored as one string, so
# e.g. Class is directly usable for set-intersection queries (confirmed
# real, 2026-08-05: an item's *effective* class list is the intersection of
# base item classes and whatever exaltation is socketed -- see project memory).
_LIST_VALUE_LABELS = {"Class", "Race", "Slot"}
# Boilerplate sentences (not label:value data) that still contain a colon
# somewhere ("This Augmentation fits in slot types: (Proc Exaltation)") --
# excluded outright rather than left to the label-length cap, which a long
# sentence fragment could still slip under.
_BOILERPLATE_LINE_RE = re.compile(r"^\s*.{0,4}\bThis\b", re.IGNORECASE)
# The lookahead (where a value ends and the next label begins) is anchored
# on the actual known label vocabulary rather than a generic "capitalized
# word, then colon" pattern -- confirmed real (2026-08-05, "Weight: 0.1
# #4Haste: 33%") that OCR sometimes glues a value directly onto the next
# label with zero separator characters (digit touching letter, so no \b
# word-boundary exists between them either), which no amount of "tolerate a
# few garbage chars" can catch. A generic pattern also false-split inside
# unrecognized words (e.g. "SMALL" -> "S" + "MALL AC" before this fix,
# because "MALL AC:" superficially looks like its own "word: word:" shape)
# -- anchoring on real label names fixes both problems at once.
_KNOWN_LABELS_ALT = "|".join(
    re.escape(l) for l in sorted(_STAT_LABELS + _EFFECT_LABELS + _META_LABELS
                                   + list(_EXALT_LABEL_CANON), key=len, reverse=True)
)
_LABEL_VALUE_RE = re.compile(
    rf"([A-Za-z][A-Za-z. ]{{1,20}}?)\s*:\s*(.+?)(?=(?:{_KNOWN_LABELS_ALT})\s*:|sv\.?\s*\w+\s*:|$)",
    re.IGNORECASE,
)


def _classify_label(raw_label: str) -> tuple[str, str]:
    """Returns (canonical_label, bucket). bucket is one of stat/save/effect/
    exaltation/meta/other -- see _extract_fields."""
    l = raw_label.strip().lower()
    if m := re.search(r"\bsv\.?\s*(\w+)", l):
        return f"SV. {m.group(1).title()}", "save"
    for known in sorted(_EXALT_LABEL_CANON, key=len, reverse=True):
        if re.search(rf"\b{re.escape(known)}\b", l):
            return _EXALT_LABEL_CANON[known], "exaltation"
    for known in sorted(_EFFECT_LABELS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(known)}\b", l):
            return known.title(), "effect"
    for known in sorted(_STAT_LABELS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(known)}\b", l):
            return (known.upper() if known in ("ac", "hp") else known.title()), "stat"
    for known in _META_LABELS:
        if re.search(rf"\b{re.escape(known)}\b", l):
            return known.title(), "meta"
    return raw_label.strip(), "other"


# Every stat/save label is a plain number except Size (SMALL/MEDIUM/...).
# Confirmed real (Runed Bolster Belt): a value can still have icon-glyph OCR
# garbage trailing it even after the label-boundary fix above correctly
# splits the *next* field out -- splitting Haste out as its own field didn't
# clean what's left in Weight's value ("0.1 #4"), because that garbage sits
# between the two, not after the whole match. Trimming to the leading
# numeric token fixes both that and the separate ask of storing Haste as a
# plain comparable number rather than a "33%" string (its practical use is
# always a minimum/threshold value, e.g. "need Haste >= 15").
# Confirmed real ("Mudman Enforcer +3"): "Skill" is a text value ("1H
# Blunt"), not numeric like the rest of _STAT_LABELS -- was getting trimmed
# down to just "1" by the leading-number trim below before this was added.
_NON_NUMERIC_STAT_LABELS = {"Size", "Skill"}
_LEADING_NUMBER_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def _extract_fields(block_text: str) -> dict:
    """Sweeps a block's text for every 'Label: Value' pair and classifies
    each into a bucket. Prefill only -- see items_review's rendering of
    this into editable per-bucket fields, still confirmed/corrected by a
    human before it counts."""
    buckets = {"meta": {}, "stat": {}, "save": {}, "effect": {}, "exaltation": {}, "other": {}}
    for line in block_text.splitlines():
        if re.search(r"\b(un)?modified\b", line, re.IGNORECASE):
            continue
        if _BOILERPLATE_LINE_RE.match(line):
            continue
        for m in _LABEL_VALUE_RE.finditer(line):
            label, bucket = _classify_label(m.group(1))
            value = m.group(2).strip()
            if bucket in ("stat", "save") and label not in _NON_NUMERIC_STAT_LABELS:
                if m2 := _LEADING_NUMBER_RE.match(value):
                    value = m2.group(1)
            if value:
                buckets[bucket][label] = value
    return buckets


def _split_item_blocks(ocr_text: str | None) -> list[dict]:
    lines = (ocr_text or "").splitlines()
    class_idxs = [i for i, line in enumerate(lines) if _CLASS_LINE_RE.match(line)]
    if not class_idxs:
        return []

    blocks = []
    for n, class_idx in enumerate(class_idxs):
        # Positional, not keyword-matched (see _LEADING_GARBAGE_RE note):
        # name is always 2 lines above Class:, flags line always 1 above.
        above = [l.strip() for l in lines[max(0, class_idx - 5):class_idx] if l.strip()]
        # A lone candidate line is ambiguous -- is it the name (no separate
        # flags line exists for this item, confirmed real: "Winter Lilly",
        # a plain reagent with no No Trade/Lore/etc. tag at all) or the
        # flags line (OCR dropped the name line entirely -- confirmed real:
        # "Rune Etched Helm +2", not garbled, just absent, likely a too-
        # tight crop with no margin above the title text)? Decided by
        # whether it matches known flag keywords -- if so, treat it as the
        # (name-less) flags line; if not, treat it as the (flags-less) name.
        # Previously used the same single line for *both* name_guess and
        # flags_guess ("above[-1]" and "above[0]" are the same element in a
        # 1-item list), which duplicated the name into Flags for items like
        # Winter Lilly that never had a flags line to begin with.
        is_single_flags_line = len(above) == 1 and bool(_POSSESSIONS_HINT_RE.search(above[0]))
        if len(above) >= 2:
            name_guess = _LEADING_GARBAGE_RE.sub("", above[-2])
            flags_guess = above[-1]
        elif is_single_flags_line:
            name_guess = ""
            flags_guess = above[0]
        elif len(above) == 1:
            name_guess = _LEADING_GARBAGE_RE.sub("", above[0])
            flags_guess = ""
        else:
            name_guess = ""
            flags_guess = ""
        # Surfaced in the review form as an explicit "retake this
        # screenshot" flag rather than just a blank name field, so it's
        # not confused with an ordinary field the reviewer just needs to
        # type in -- the name genuinely isn't recoverable from this capture.
        missing_title = is_single_flags_line

        # Slot is the first non-blank, no-colon line after Race: (bare word
        # like "Waist"/"Primary Secondary"/"Range", confirmed real) --
        # search from class_idx forward rather than assuming a fixed offset
        # since a stray OCR line can land between Race: and the slot line.
        # Confirmed real (2026-08-05, "Polished Mithril Mask (Exaltation)"):
        # some captures interleave a blank line after every meta line
        # ("Race: ALL", "", "Face"), so the inner loop has to skip blank
        # lines rather than give up on the first one -- the old unconditional
        # break exited after exactly one line checked, always the blank one.
        # Confirmed real ("Winter Lilly", 2026-08-05): a non-equippable item
        # (Class: None, Race: None) has no slot line at all -- Race: is
        # followed directly by the stat block. The old loop skipped past
        # colon-having lines (Size:/Weight:) instead of stopping at them,
        # so it kept searching until it hit the next bare, no-colon line --
        # "Mi unmodified" -- and wrongly took that as the slot. Fixed to
        # give up as soon as the stat block (or the Modified/Unmodified
        # line) starts, since a real slot line always appears before both.
        slot_guess = ""
        for i in range(class_idx, min(class_idx + 6, len(lines))):
            if re.search(r"\brace\s*[:;]", lines[i], re.IGNORECASE):
                for j in range(i + 1, min(i + 5, len(lines))):
                    candidate = lines[j].strip()
                    if not candidate:
                        continue
                    if ":" in candidate or re.search(r"\b(un)?modified\b", candidate, re.IGNORECASE):
                        break
                    slot_guess = candidate
                    break
                break

        # Block ends at the next item's name line (a few lines before its
        # Class:), the first line that looks like guild/tell chat, or a
        # fixed line cap -- whichever comes first.
        next_start = class_idxs[n + 1] - 5 if n + 1 < len(class_idxs) else len(lines)
        end = min(next_start, class_idx + MAX_BLOCK_LINES, len(lines))
        start = max(0, class_idx - 2)
        for i in range(start, end):
            if _CHAT_LINE_RE.search(lines[i]):
                end = i
                break

        # "©" for "0" is a consistent OCR misread in Weight values on every
        # capture seen so far (e.g. "Weight: ©.1") -- a copyright symbol has
        # no legitimate reason to appear in item stat text, so this is safe
        # to normalize globally rather than only within a matched value.
        block_text = "\n".join(l for l in lines[start:end] if l.strip()).replace("©", "0")
        guess = _guess_tier_fields(block_text)

        # Cross-check against the name's own "+N" -- fills in a missing
        # tier guess, or flags a mismatch worth a second look (OCR read one
        # of the two wrong, since they're supposed to be the same number).
        # Confirmed real: every item without a "+N" in its name is Tier 0 --
        # there's no such thing as a base item that's secretly a higher
        # tier -- so "no +" implies tier 0 exactly as reliably as "+N"
        # implies tier N. Extends the cross-check to catch base-item
        # misreads too (confirmed real: "Small Ornate Chain Coif" OCR'd as
        # Tier 6 with nothing to catch it before this, since the old check
        # only fired when the name actually had a "+"). Skipped when the
        # name itself wasn't recoverable (missing_title) -- nothing to
        # cross-check against then.
        tier_mismatch = None
        if m := _NAME_TIER_SUFFIX_RE.search(name_guess):
            name_tier = m.group(1)
        elif name_guess and not missing_title:
            name_tier = "0"
        else:
            name_tier = None
        if name_tier is not None:
            if not guess["tier"]:
                guess["tier"] = name_tier
            elif guess["tier"] != name_tier:
                tier_mismatch = f'name suggests tier {name_tier}, OCR read tier {guess["tier"]} -- check screenshot'

        # Confirmed real: tier progress is "X/Y" where X is progress toward
        # completing the current tier and Y is the threshold needed to
        # advance to the next one -- and Y is always 2^tier (verified
        # against every real capture so far: Tier2->4, Tier3->8, Tier5->32,
        # Tier6->64), resetting X to 0 at the start of each new tier. Only
        # meaningful for tiers 0-9 -- Tier 10 is the maxed "N / N" display
        # (see _guess_tier_fields), not a literal 2^10 threshold. Flags a
        # mismatch the same way as the name/tier cross-check, since Y is
        # just as derivable from tier as tier is from the name's "+N".
        progress_expected_denom = None
        if guess["tier"] and guess["tier"].isdigit() and int(guess["tier"]) < 10:
            progress_expected_denom = 2 ** int(guess["tier"])
            if guess["tier_progress"]:
                pm = re.match(r"\d+\s*/\s*(\d+)", guess["tier_progress"])
                if pm and int(pm.group(1)) != progress_expected_denom:
                    progress_msg = (f'tier {guess["tier"]} implies progress denominator '
                                     f'{progress_expected_denom}, OCR read {pm.group(1)} -- check screenshot')
                    tier_mismatch = f'{tier_mismatch}; {progress_msg}' if tier_mismatch else progress_msg

        fields = _extract_fields(block_text)
        # Flags/slot aren't "Label: Value" lines (flags has no colon at all
        # in the OCR text most of the time; slot is a bare word), so
        # _extract_fields never finds them -- added in here explicitly so
        # they still land in the editable review-form table instead of
        # being silently discarded.
        if flags_guess:
            fields["meta"]["Flags"] = _clean_flags_line(flags_guess)
        if slot_guess:
            fields["meta"]["Slot"] = slot_guess
        # Exaltation slots found by the generic sweep fill in anything the
        # dedicated 5-slot guesser (which anchors on exact known label text)
        # missed, without overriding what it already found.
        exalt_slots = _guess_exaltation_slots(block_text)
        for label, value in fields["exaltation"].items():
            key = label.lower().replace(" ", "_")
            if key in exalt_slots and not exalt_slots[key]:
                exalt_slots[key] = value

        blocks.append({
            "name_guess": name_guess,
            "block_text": block_text,
            "tier_mismatch": tier_mismatch,
            "missing_title": missing_title,
            "progress_expected_denom": progress_expected_denom,
            "fields": fields,
            **guess,
            **exalt_slots,
        })
    return blocks


@app.get("/items", response_class=HTMLResponse)
def items_list(request: Request, search: str = ""):
    # One row per *base item* (tier variants of the same item -- "Coif" /
    # "Coif +1" / ... -- grouped together, see _base_item_name), showing the
    # lowest tier/progress snapshot by default rather than the most recent
    # confirm. item_info stays an append-only history underneath; full
    # per-tier browsing is on /items/detail's dropdown, and pure re-confirm
    # drift auditing for one exact tier is on /items/history.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM item_info")
            all_rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS c FROM item_captures WHERE status='pending'")
            pending_count = cur.fetchone()["c"]
    finally:
        conn.close()

    families: dict[str, list] = {}
    for r in all_rows:
        families.setdefault(_base_item_name(r["item"]), []).append(r)

    rows = []
    for base_name, family in families.items():
        family.sort(key=_tier_sort_key)
        default = family[0]
        default["screenshot_url"] = _item_screenshot_url(default["screenshot_path"])
        default["stats"] = _parse_stats_json(default["stats"])
        default["snapshot_count"] = len(family)
        default["base_name"] = base_name
        rows.append(default)
    rows.sort(key=lambda r: r["base_name"])

    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in r["base_name"].lower()]

    return templates.TemplateResponse(request, "items.html", {
        "rows": rows, "search": search, "pending_count": pending_count,
    })


# Display-time grouping of the "stat" bucket into the item window's own
# columns (confirmed real, 2026-08-05: top section is Size/Weight/Skill/
# Ratio on the left, AC/HP/Mana/Haste/etc. on the right; middle section is
# character stats / saves / regens as three side-by-side columns) -- purely
# a rendering split, not a storage one, since collapsing them into one
# "stat" bucket for indexing doesn't need to give up the layout a human
# recognizes at a glance when sanity-checking a page against the real item.
_TOP_LEFT_STATS = ("Size", "Weight", "Skill", "Ratio")
_CHAR_STAT_LABELS = ("Strength", "Stamina", "Agility", "Dexterity", "Wisdom", "Intelligence", "Charisma")
_REGEN_LABELS = ("Hp Regen", "Mana Regen", "End Regen", "Endurance Regen")


def _grouped_stats(stats: dict) -> dict:
    groups = {"top_left": {}, "top_right": {}, "char": {}, "regen": {}}
    for k, v in stats.items():
        if k in _TOP_LEFT_STATS:
            groups["top_left"][k] = v
        elif k in _CHAR_STAT_LABELS:
            groups["char"][k] = v
        elif k in _REGEN_LABELS:
            groups["regen"][k] = v
        else:
            groups["top_right"][k] = v
    return groups


@app.get("/items/detail", response_class=HTMLResponse)
def items_detail(request: Request, item: str, id: int | None = None):
    base_name = _base_item_name(item)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # Small catalog (hand-curated, one screenshot at a time) -- a
            # full-table fetch + Python-side base-name grouping is simpler
            # and more correct than fighting MariaDB's REGEXP_REPLACE across
            # versions for what's realistically never more than a few
            # hundred rows.
            cur.execute("SELECT * FROM item_info")
            all_rows = cur.fetchall()
            cur.execute("SELECT DISTINCT item FROM item_info ORDER BY item")
            known_items = [r["item"] for r in cur.fetchall()]
    finally:
        conn.close()

    family = [r for r in all_rows if _base_item_name(r["item"]) == base_name]
    if not family:
        raise HTTPException(status_code=404, detail=f"No confirmed item named {item!r}")
    family.sort(key=_tier_sort_key)

    latest = next((r for r in family if r["id"] == id), None) if id is not None else family[0]
    if latest is None:
        raise HTTPException(status_code=404, detail=f"No snapshot {id} for {item!r}")

    latest["screenshot_url"] = _item_screenshot_url(latest["screenshot_path"])
    stats = _parse_stats_json(latest["stats"])
    # stats JSON is already flat (meta+stat+save+effect all merged by
    # items_confirm) -- meta/save/effect keys are pulled out by name below
    # rather than re-deriving buckets, since the bucket boundary only
    # existed transiently in the review form.
    meta_keys = ("Class", "Race", "Slot", "Flags")
    save_keys = tuple(k for k in stats if k.startswith("SV."))
    effect_keys = tuple(k for k in stats if "Effect" in k)
    meta = {k: stats[k] for k in meta_keys if k in stats}
    saves = {k: stats[k] for k in save_keys}
    effects = {k: stats[k] for k in effect_keys}
    remaining_stats = {k: v for k, v in stats.items()
                        if k not in meta_keys and k not in save_keys and k not in effect_keys}
    grouped = _grouped_stats(remaining_stats)
    grouped["save"] = saves

    # Every snapshot in the family (every tier, and every distinct progress
    # within a tier -- confirmed real that progress alone can change stats,
    # not just crossing a tier boundary) becomes its own dropdown option,
    # not just one entry per tier.
    variants = [{
        "id": r["id"],
        "item": r["item"],
        "label": (
            (f'Tier {r["tier"]}' + (f' ({r["tier_progress"]})' if r["tier_progress"] else ''))
            if r["tier"] is not None else '(no tier)'
        ) + f' -- confirmed {r["confirmed_at"]}',
        "selected": r["id"] == latest["id"],
    } for r in family]

    # Drop info -- reuses the same loot search /loot/eql already does,
    # scoped to just this snapshot's own exact item name (tier variants
    # have different loot-event names too, since the "+N" is part of the
    # dropped item's name).
    loot_rows, loot_truncated = _compute_loot("eql", item=latest["item"])

    return templates.TemplateResponse(request, "items_detail.html", {
        "item": latest["item"], "base_name": base_name, "variants": variants,
        "latest": latest, "meta": meta, "grouped": grouped, "stats": stats,
        "effects": effects, "exaltations": latest["exaltations"],
        "loot_rows": loot_rows, "loot_truncated": loot_truncated,
        "known_items": known_items,
    })


@app.post("/items/update")
def items_update(
    id: int = Form(...), item: str = Form(...),
    tier: str = Form(""), tier_progress: str = Form(""), upgradeable: str = Form(""),
    exaltations: str = Form(""),
    field_label: list[str] = Form([]), field_value: list[str] = Form([]),
    note: str = Form(""),
):
    # True UPDATE, not another append-only snapshot -- deliberately
    # different from /items/confirm. The append-only design exists to
    # preserve genuine tier/progress *progression* (see schema.sql); a
    # typo or a missed field on an already-confirmed snapshot isn't
    # progression, it's a mistake, and permanently cluttering that item's
    # history dropdown with a "wrong" entry to fix it would undermine the
    # exact thing append-only history is for. A real tier/progress change
    # still goes through a new screenshot + /items/confirm as normal.
    stats = {}
    for label, value in zip(field_label, field_value):
        label, value = label.strip(), value.strip()
        if not label or not value:
            continue
        stats[label] = value.split() if label in _LIST_VALUE_LABELS else value
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE item_info SET item=%s, tier=%s, tier_progress=%s, upgradeable=%s, "
                "exaltations=%s, stats=%s, note=%s WHERE id=%s",
                (
                    item,
                    int(tier) if tier.strip().isdigit() else None,
                    tier_progress or None,
                    {"yes": 1, "no": 0}.get(upgradeable),
                    exaltations or None,
                    json.dumps(stats) if stats else None,
                    note or None,
                    id,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(f"/items/detail?item={quote(item)}&id={id}", status_code=303)


@app.post("/items/delete")
def items_delete(id: int = Form(...), item: str = Form(...)):
    # A genuine DELETE, distinct from both /items/update (fixes a field on
    # a snapshot that should still exist) and the append-only design of
    # /items/confirm (every real confirm is deliberately kept forever) --
    # this is specifically for a snapshot that shouldn't have been
    # confirmed at all (e.g. an accidental duplicate double-submit).
    base_name = _base_item_name(item)
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM item_info WHERE id=%s", (id,))
            cur.execute("SELECT item FROM item_info")
            remaining = cur.fetchall()
        conn.commit()
    finally:
        conn.close()
    sibling = next((r["item"] for r in remaining if _base_item_name(r["item"]) == base_name), None)
    if sibling:
        return RedirectResponse(f"/items/detail?item={quote(sibling)}", status_code=303)
    return RedirectResponse("/items", status_code=303)


@app.get("/items/history", response_class=HTMLResponse)
def items_history(request: Request, item: str):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM item_info WHERE item=%s ORDER BY confirmed_at", (item,))
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        r["screenshot_url"] = _item_screenshot_url(r["screenshot_path"])
        r["stats"] = _parse_stats_json(r["stats"])
    return templates.TemplateResponse(request, "items_history.html", {"item": item, "rows": rows})


@app.get("/items/review", response_class=HTMLResponse)
def items_review(request: Request):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM item_captures WHERE status='pending' ORDER BY captured_at")
            rows = cur.fetchall()
            # Distinct already-confirmed item names, offered as an
            # autocomplete on the item name + exaltation slot fields below --
            # confirmed real that OCR spells the same exaltation name
            # differently across captures (see _EXALTATION_SLOTS), so
            # picking the existing spelling instead of retyping a fresh OCR
            # guess keeps them joinable.
            cur.execute("SELECT DISTINCT item FROM item_info ORDER BY item")
            known_items = [r["item"] for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["screenshot_url"] = _item_screenshot_url(r["screenshot_path"])
        r["blocks"] = _split_item_blocks(r["raw_ocr_text"])
    return templates.TemplateResponse(request, "items_review.html", {"rows": rows, "known_items": known_items})


@app.post("/items/confirm")
def items_confirm(
    capture_id: int = Form(...), item: str = Form(...),
    tier: str = Form(""), tier_progress: str = Form(""), upgradeable: str = Form(""),
    ornamentation: str = Form(""), focus_exaltation: str = Form(""),
    click_exaltation: str = Form(""), worn_exaltation: str = Form(""), proc_exaltation: str = Form(""),
    field_label: list[str] = Form([]), field_value: list[str] = Form([]),
    note: str = Form(""),
):
    # Confirm also dismisses the capture (2026-08-05: previously they were
    # separate steps -- confirm stayed on /items/review with the same
    # capture's form still showing the original, possibly-since-corrected
    # OCR guess again, which read as "did my edit not save?" even though it
    # had). Multi-item captures are rare now that screenshots are single-
    # item and manually curated -- if a capture genuinely has more than one
    # item, submit every block's form while the page is still open (before
    # the first submit navigates away) rather than relying on the capture
    # staying in the queue afterward.
    exaltations = "\n".join(
        f"{label}: {value}" for label, value in (
            ("Ornamentation", ornamentation), ("Focus Exaltation", focus_exaltation),
            ("Click Exaltation", click_exaltation), ("Worn Exaltation", worn_exaltation),
            ("Proc Exaltation", proc_exaltation),
        ) if value.strip()
    )
    # field_label[]/field_value[] are the review form's editable rows for
    # every auto-extracted stat/save/effect/meta field (plus any blank rows
    # added by hand) -- collapsed into one flat, queryable JSON object
    # rather than kept as free text (see schema.sql's note on item_info.stats).
    # Class/Race/Slot are stored as arrays, not one space-separated string --
    # confirmed real that computing "can my loadout use this" is a set
    # question (base classes ∩ socketed exaltation's classes), which needs
    # actual list values to query, not text that has to be re-split later.
    stats = {}
    for label, value in zip(field_label, field_value):
        label, value = label.strip(), value.strip()
        if not label or not value:
            continue
        stats[label] = value.split() if label in _LIST_VALUE_LABELS else value
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT screenshot_path FROM item_captures WHERE id=%s", (capture_id,))
            cap = cur.fetchone()
            cur.execute(
                "INSERT INTO item_info "
                "(item, tier, tier_progress, upgradeable, exaltations, stats, note, screenshot_path, source_capture_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    item,
                    int(tier) if tier.strip().isdigit() else None,
                    tier_progress or None,
                    {"yes": 1, "no": 0}.get(upgradeable),
                    exaltations or None,
                    json.dumps(stats) if stats else None,
                    note or None,
                    cap["screenshot_path"] if cap else None,
                    capture_id,
                ),
            )
            cur.execute(
                "UPDATE item_captures SET status='confirmed', reviewed_at=NOW() WHERE id=%s",
                (capture_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/items/review", status_code=303)


@app.post("/items/dismiss")
def items_dismiss(capture_id: int = Form(...)):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE item_captures SET status='confirmed', reviewed_at=NOW() WHERE id=%s",
                (capture_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/items/review", status_code=303)


@app.post("/items/reject")
def items_reject(capture_id: int = Form(...)):
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE item_captures SET status='rejected', reviewed_at=NOW() WHERE id=%s",
                (capture_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/items/review", status_code=303)


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


def _compute_dialogue(game, character="", npc="", zone="", exclude_chatter=False):
    # Classic EQ quests aren't structured turn-ins like EQ2's -- they're
    # branching NPC dialogue (bracketed [keywords] you echo back) plus
    # whatever you're handed in return. There's no logged "give item to
    # NPC" action at all (confirmed empty across a 425k-line real log), so
    # this can't produce a single "quest completed" row the way EQ2's does;
    # instead it's a flat chronological transcript of hail/say/npc_dialogue/
    # reward events, filterable by npc/zone, so a real dialogue tree emerges
    # from repeated visits over time. zone is correlated the same way
    # /loot, /gathering, and EQ2's /quests already do.
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


@app.get("/attacks/eq", response_class=HTMLResponse)
def attacks_breakdown_eq(request: Request, character: str = "", start_ts: str = "", end_ts: str = ""):
    return _render_attacks(request, "eq", "EverQuest", character, start_ts, end_ts)


@app.get("/attacks/eq2", response_class=HTMLResponse)
def attacks_breakdown_eq2(request: Request, character: str = "", start_ts: str = "", end_ts: str = ""):
    return _render_attacks(request, "eq2", "EverQuest II", character, start_ts, end_ts)


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


@app.get("/zones/eq2", response_class=HTMLResponse)
def zones_report_eq2(request: Request, character: str = "", zone: str = "", start_ts: str = "", end_ts: str = ""):
    searched = bool(character or zone or start_ts or end_ts)
    rows = _compute_zones("eq2", character, zone, start_ts, end_ts) if searched else []
    return templates.TemplateResponse(request, "zones.html", {
        "rows": rows, "game": "eq2", "game_label": "EverQuest II",
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
    # Computes base_zone once per matching con/npc_dialogue row (via a
    # single GROUP BY query) rather than once per (zone, row) pair via a
    # per-zone loop -- the previous per-zone-loop version measured ~33s for
    # 50 zones (each zone re-scanning and re-correlating every con/dialogue
    # row from scratch); this version is under a second. See
    # _compute_zone_connections's docstring for the same lesson applied to
    # the gate-exclusion check.
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(e.extra,'$.base_zone')) AS zone "
                "FROM events e JOIN games g ON e.game_id=g.id "
                "WHERE e.event_type='zone_change' AND g.code=%s",
                (game,),
            )
            zones = sorted(r["zone"] for r in cur.fetchall() if r["zone"])

            cur.execute(
                "SELECT ev.source_name AS npc, ev.amount AS level, "
                "  JSON_EXTRACT(ev.extra,'$.rare')=true AS rare, "
                f"  {_base_zone_expr()} AS zone "
                "FROM events ev JOIN games g ON ev.game_id=g.id "
                "WHERE ev.event_type='con' AND g.code=%s",
                (game,),
            )
            con_rows = [r for r in cur.fetchall() if r["zone"]]

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

            con_by_zone = {}
            for r in con_rows:
                entry = con_by_zone.setdefault(r["zone"], {"levels": [], "rare_names": set()})
                if r["rare"]:
                    entry["rare_names"].add(r["npc"].lower())
                if r["npc"].lower() in fought_names:
                    entry["levels"].append(r["level"])

            cur.execute(
                "SELECT zone, COUNT(DISTINCT source_name) AS npc_count FROM ("
                "  SELECT ev.source_name, "
                f"    {_base_zone_expr()} AS zone "
                "  FROM events ev JOIN games g ON ev.game_id=g.id "
                "  WHERE ev.event_type IN ('con','npc_dialogue') AND g.code=%s"
                ") t WHERE zone IS NOT NULL GROUP BY zone",
                (game,),
            )
            npc_by_zone = {r["zone"]: r["npc_count"] for r in cur.fetchall()}

            cur.execute("SELECT zone, level_min_override, level_max_override, note FROM zone_info")
            overrides = {r["zone"]: r for r in cur.fetchall()}
    finally:
        conn.close()

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
            cur.execute(
                "WITH zone_npc_events AS ("
                "  SELECT ev.source_name AS npc, ev.event_type, ev.amount, ev.extra "
                "  FROM events ev JOIN games g ON ev.game_id=g.id "
                "  WHERE ev.event_type IN ('con','npc_dialogue') AND ev.source_name IS NOT NULL "
                f"    AND g.code=%s AND {match_where} "
                "  UNION ALL "
                "  SELECT ev.target_name AS npc, ev.event_type, ev.amount, ev.extra "
                "  FROM events ev JOIN games g ON ev.game_id=g.id "
                "  WHERE ev.event_type='death' AND ev.target_type != 'you' AND ev.target_name IS NOT NULL "
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
        "rares": rares,
        "override": override,
    }


@app.get("/zoneinfo/eql", response_class=HTMLResponse)
def zoneinfo_list_eql(request: Request):
    rows = _compute_zone_list("eql")
    return templates.TemplateResponse(request, "zoneinfo_list.html", {
        "rows": rows, "game": "eql", "game_label": "EQ Legends",
    })


@app.get("/zoneinfo/eq2", response_class=HTMLResponse)
def zoneinfo_list_eq2(request: Request):
    rows = _compute_zone_list("eq2")
    return templates.TemplateResponse(request, "zoneinfo_list.html", {
        "rows": rows, "game": "eq2", "game_label": "EverQuest II",
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


@app.get("/zoneinfo/eq2/detail", response_class=HTMLResponse)
def zoneinfo_detail_eq2(request: Request, zone: str, tier: str = "all", solo: str = "any"):
    return _render_zoneinfo_detail(request, "eq2", "EverQuest II", zone, tier, solo)


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
    sql = (
        "WITH npc_events AS ("
        "  SELECT ev.source_name AS npc, ev.event_type, ev.amount, ev.extra, "
        f"    {_base_zone_expr()} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id "
        "  WHERE g.code=%s AND ev.event_type IN ('con','npc_dialogue','vendor_buy','vendor_sell','loot') "
        "    AND ev.source_name IS NOT NULL "
        "  UNION ALL "
        "  SELECT ev.target_name AS npc, ev.event_type, ev.amount, ev.extra, "
        f"    {_base_zone_expr()} AS zone "
        "  FROM events ev JOIN games g ON ev.game_id=g.id "
        "  WHERE g.code=%s AND ev.event_type='death' AND ev.target_name IS NOT NULL AND ev.target_type != 'you'"
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


@app.get("/npcs/eq2", response_class=HTMLResponse)
def npcs_list_eq2(request: Request, search: str = "", zone: str = ""):
    rows = _compute_npc_list("eq2", search, zone) if (search or zone) else []
    return templates.TemplateResponse(request, "npc_list.html", {
        "rows": rows, "game": "eq2", "game_label": "EverQuest II",
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


@app.get("/npcs/eq2/detail", response_class=HTMLResponse)
def npc_detail_eq2(request: Request, npc: str, tier: str = "all"):
    detail = _compute_npc_detail("eq2", npc, tier)
    return templates.TemplateResponse(request, "npc_detail.html", {
        "game": "eq2", "game_label": "EverQuest II", **detail,
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

        encounters = _merge_encounters(markers) + _derive_gap_based_encounters(activity, EQL_OUT_OF_COMBAT_SECONDS)

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
    NPC attacking the combatant) -- the /attacks page's per-verb
    granularity, scoped to the given window(s) and not just to 'you'. Each
    window is OR'd into a single query, so rows from several (possibly
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
            "dodged": 0, "parried": 0, "blocked": 0, "riposted": 0, "resisted": 0,
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

    def pct(n, den):
        return round(n / den * 100, 1) if den else None

    def shape(b):
        attempts, landed = b["attempts"], b["landed"]
        hit_amts, crit_amts = b["hit_amounts"], b["crit_amounts"]
        row = {k: v for k, v in b.items() if k not in (
            "attempts", "landed", "crits", "misses", "dodged", "parried",
            "blocked", "riposted", "resisted", "hit_amounts", "crit_amounts",
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

    return templates.TemplateResponse(request, "encounter_breakdown.html", {
        "npcs": npcs,
        "damage_totals": None,
        "healers": healers,
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

    return templates.TemplateResponse(request, "encounter_breakdown.html", {
        "npcs": [],
        "damage_totals": damage_totals,
        "healers": healers,
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
        log_roots.get("eql", ""), log_roots.get("eq2", ""), log_roots.get("eq", "")
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
