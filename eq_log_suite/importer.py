"""Bulk-import an EQ-family log file into MySQL.

Usage:
    python -m eq_log_suite.importer /home/myself/eql/Logs/eqlog_Cheerful_rivervale.txt

Resumable: each log_sources row tracks last_byte_offset, so re-running only
picks up bytes appended since the last run (the same mechanism the live
tailer uses to catch up after a restart).
"""

import argparse
import sys
from pathlib import Path

from eq_log_suite import db, ingest
from eq_log_suite.parsers.registry import detect_parser, get_parser

BATCH_SIZE = 500

GAME_NAMES = {"eql": "EverQuest Legends", "eq2": "EverQuest II"}


def guess_character_from_filename(path: str) -> str | None:
    # classic EQ naming: eqlog_<Character>_<Server>.txt
    parts = Path(path).stem.split("_")
    if len(parts) >= 2 and parts[0].lower() == "eqlog":
        return parts[1]
    return None


def guess_server_from_filename(path: str) -> str | None:
    parts = Path(path).stem.split("_")
    if len(parts) >= 3 and parts[0].lower() == "eqlog":
        return "_".join(parts[2:])
    return None


def import_file(path: str, game_code: str | None, character_name: str | None, server: str | None, mark_live: bool = False):
    if game_code is None or game_code == "auto":
        parser_cls = detect_parser(path)
        if parser_cls is None:
            print(f"Could not auto-detect game for {path}; pass --game explicitly.", file=sys.stderr)
            sys.exit(1)
    else:
        parser_cls = get_parser(game_code)

    character_name = character_name or guess_character_from_filename(path) or "Unknown"
    server = server or guess_server_from_filename(path)

    conn = db.get_connection()
    game_id = db.get_or_create_game(
        conn, parser_cls.game_code, GAME_NAMES.get(parser_cls.game_code, parser_cls.game_code)
    )
    character_id = db.get_or_create_character(conn, game_id, character_name, server)
    log_source = db.get_or_create_log_source(conn, game_id, character_id, path, live=mark_live)
    log_source_id = log_source["id"]
    start_offset = log_source["last_byte_offset"]

    total_count = 0
    parsed_count = 0
    rare_tagger = ingest.RareGatherTagger()

    with open(path, "rb") as f:
        # Recompute the starting line number by counting newlines already
        # consumed on a prior run -- cheap relative to the parse itself.
        prefix = f.read(start_offset)
        line_no = prefix.count(b"\n")
        f.seek(start_offset)

        offset = start_offset
        events_batch: list = []
        raw_batch: list = []

        def flush():
            nonlocal events_batch, raw_batch
            ingest.insert_batch(conn, game_id, character_id, log_source_id, events_batch, raw_batch)
            events_batch = []
            raw_batch = []

        for raw_bytes in f:
            offset += len(raw_bytes)
            line = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            total_count += 1
            idx = total_count

            event = rare_tagger.apply(parser_cls.parse_line(line, line_no=line_no))
            if event is not None:
                parsed_count += 1
                events_batch.append((idx, event))
                raw_batch.append((idx, line_no, event.ts, line))
            else:
                ts_only = parser_cls.parse_timestamp(line)
                raw_batch.append((idx, line_no, ts_only[0] if ts_only else None, line))
            line_no += 1

            if len(raw_batch) >= BATCH_SIZE:
                flush()
                db.update_log_source_offset(conn, log_source_id, offset)

        flush()
        db.update_log_source_offset(conn, log_source_id, offset)

    print(f"Imported {path}: {total_count} new lines read, {parsed_count} parsed into events (resumed from byte offset {start_offset}).")


def main():
    ap = argparse.ArgumentParser(description="Bulk-import an EQ-family log file into MySQL.")
    ap.add_argument("path")
    ap.add_argument("--game", default="auto", help="eql | eq2 | auto (default: auto-detect)")
    ap.add_argument("--character", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument("--live", action="store_true", help="mark this log_source for live tailing")
    args = ap.parse_args()
    import_file(args.path, args.game, args.character, args.server, mark_live=args.live)


if __name__ == "__main__":
    main()
