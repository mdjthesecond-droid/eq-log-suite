"""Shared DB-write path used by both the bulk importer and the live tailer,
so historical import and live catch-up never diverge in behavior."""

import json


class RareGatherTagger:
    """Correlates EQ2's 'You have found a rare item!' marker line with the
    gather event that immediately follows it, tagging that gather event's
    `outcome` (e.g. 'rare') so rarity is queryable directly on the gather
    row instead of needing a manual line-adjacency join later.

    Stateful across a sequential run through one log source's lines --
    each importer run / tailer task should use its own instance.
    """

    def __init__(self):
        self._pending_tier = None

    def apply(self, event):
        if event is None:
            return event
        if event.event_type == "rare_found":
            self._pending_tier = event.outcome
            return event
        if self._pending_tier is not None:
            if event.event_type == "gather":
                event.outcome = self._pending_tier
            # Only the line immediately after the marker counts, matched or not.
            self._pending_tier = None
        return event


def insert_batch(conn, game_id, character_id, log_source_id, events_batch, raw_batch):
    """events_batch: list of (idx, ParsedEvent).
    raw_batch: list of (idx, line_no, ts, raw_text) -- one entry per line read,
    whether or not it parsed into an event.

    Sets `.db_id` on each ParsedEvent in events_batch to its inserted row id.
    """
    if not raw_batch:
        return
    with conn.cursor() as cur:
        event_id_by_idx = {}
        for idx, event in events_batch:
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
            event.db_id = cur.lastrowid
            event_id_by_idx[idx] = cur.lastrowid
        for idx, line_no, ts, raw_text in raw_batch:
            cur.execute(
                "INSERT INTO raw_lines (log_source_id, line_no, ts, raw_text, event_id) "
                "VALUES (%s,%s,%s,%s,%s)",
                (log_source_id, line_no, ts, raw_text, event_id_by_idx.get(idx)),
            )
    conn.commit()
