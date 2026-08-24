"""Re-parse a log_source's currently-unmatched raw_lines (event_id IS NULL)
against the current parser, inserting any newly-matched events and linking
them back to their raw_lines row.

Use this after extending a parser's PATTERNS (e.g. adding gathering
coverage) so lines ingested before that coverage existed get backfilled
without re-tailing or re-importing the whole file -- this is exactly what
raw_lines exists for.

Usage:
    python -m eq_log_suite.backfill <log_source_id>
    python -m eq_log_suite.backfill --all
"""

import argparse
import json

from eq_log_suite import db
from eq_log_suite.parsers.registry import get_parser

BATCH_SIZE = 500


def backfill(log_source_id: int):
    conn = db.get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM log_sources WHERE id=%s", (log_source_id,))
        log_source = cur.fetchone()
        if log_source is None:
            raise SystemExit(f"no log_sources row with id={log_source_id}")
        cur.execute("SELECT code FROM games WHERE id=%s", (log_source["game_id"],))
        game_code = cur.fetchone()["code"]

    parser_cls = get_parser(game_code)
    game_id = log_source["game_id"]
    character_id = log_source["character_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, line_no, raw_text FROM raw_lines "
            "WHERE log_source_id=%s AND event_id IS NULL ORDER BY line_no",
            (log_source_id,),
        )
        rows = cur.fetchall()

    print(f"[{log_source['file_path']}] scanning {len(rows)} previously-unmatched lines...")
    newly_matched = 0
    batch: list = []

    def flush():
        nonlocal newly_matched, batch
        if not batch:
            return
        with conn.cursor() as cur:
            for raw_line_id, event in batch:
                cur.execute(
                    "INSERT INTO events (game_id, character_id, log_source_id, ts, event_type, "
                    "source_name, source_type, target_name, target_type, verb, amount, outcome, "
                    "extra, raw_line, line_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        game_id, character_id, log_source_id, event.ts, event.event_type,
                        event.source_name, event.source_type, event.target_name, event.target_type,
                        event.verb, event.amount, event.outcome,
                        json.dumps(event.extra) if event.extra else None,
                        event.raw_line, event.line_no,
                    ),
                )
                cur.execute("UPDATE raw_lines SET event_id=%s WHERE id=%s", (cur.lastrowid, raw_line_id))
                newly_matched += 1
        conn.commit()
        batch = []

    for row in rows:
        event = parser_cls.parse_line(row["raw_text"], line_no=row["line_no"])
        if event is not None:
            batch.append((row["id"], event))
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()

    print(f"[{log_source['file_path']}] backfilled {newly_matched} new events.")


def main():
    ap = argparse.ArgumentParser(description="Re-parse a log source's unmatched lines after extending its parser.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("log_source_id", type=int, nargs="?", help="a specific log_sources.id")
    group.add_argument("--all", action="store_true", help="backfill every known log source")
    args = ap.parse_args()

    if args.all:
        conn = db.get_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM log_sources")
            ids = [row["id"] for row in cur.fetchall()]
        for log_source_id in ids:
            backfill(log_source_id)
    else:
        backfill(args.log_source_id)


if __name__ == "__main__":
    main()
