"""Manage gathering "eras" -- timestamp boundaries used to re-baseline
/gathering after something changes drop rates/yields (a game patch, etc.)
without ever deleting the underlying gather data. See schema.sql's
gather_eras table for the concept; the /gathering web page defaults to
showing only the active era but can show any era or all-time.

Usage:
    python -m eq_log_suite.gather_eras list
    python -m eq_log_suite.gather_eras new "Post Patch 7.2" [--note "..."] [--at "2026-08-01 00:00:00"]
"""

import argparse
from datetime import datetime

from eq_log_suite import db


def list_eras():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM gather_eras ORDER BY started_at")
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print("No eras yet.")
        return
    for r in rows:
        status = "ACTIVE" if r["ended_at"] is None else "closed"
        note = f"  -- {r['note']}" if r["note"] else ""
        print(f"[{r['id']}] {r['name']} ({status}): {r['started_at']} -> {r['ended_at'] or '...'}{note}")


def new_era(name: str, note: str | None, at: str | None) -> str:
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            boundary = at or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            cur.execute("UPDATE gather_eras SET ended_at=%s WHERE ended_at IS NULL", (boundary,))
            cur.execute(
                "INSERT INTO gather_eras (name, started_at, ended_at, note) VALUES (%s,%s,NULL,%s)",
                (name, boundary, note),
            )
        conn.commit()
    finally:
        conn.close()
    return boundary


def main():
    ap = argparse.ArgumentParser(description="Manage gathering eras.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_new = sub.add_parser("new")
    p_new.add_argument("name")
    p_new.add_argument("--note", default=None)
    p_new.add_argument("--at", default=None, help="boundary timestamp (default: now)")
    args = ap.parse_args()

    if args.cmd == "list":
        list_eras()
    elif args.cmd == "new":
        boundary = new_era(args.name, args.note, args.at)
        print(f"Started new era '{args.name}' at {boundary}.")


if __name__ == "__main__":
    main()
