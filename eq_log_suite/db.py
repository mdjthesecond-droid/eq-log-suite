import functools
from pathlib import Path

import pymysql
import pymysql.cursors
import yaml
from dbutils.pooled_db import PooledDB

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "local.yaml"


@functools.lru_cache
def config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found -- copy config/local.example.yaml to "
            "config/local.yaml and fill in your database password."
        )
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


@functools.lru_cache
def _pool() -> PooledDB:
    # A real pool instead of a fresh pymysql.connect() (full TCP + auth
    # handshake) on every single call -- matters most for the web app, which
    # was opening one from scratch on every request, including /live's
    # once-a-second poll. The tailer/importer just check one connection out
    # for their whole task lifetime, same as before pooling existed.
    cfg = config()["database"]
    return PooledDB(
        creator=pymysql,
        mincached=1,
        maxcached=5,
        maxconnections=10,
        blocking=True,
        ping=1,  # verify a cached connection is still alive before handing it out
        host=cfg.get("host", "localhost"),
        port=cfg.get("port", 3306),
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["name"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        charset="utf8mb4",
    )


def get_connection():
    """Returns a pooled connection -- callers should still call conn.close()
    when done (this returns it to the pool rather than closing the socket)."""
    return _pool().connection()


def get_or_create_game(conn, code: str, name: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM games WHERE code=%s", (code,))
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute("INSERT INTO games (code, name) VALUES (%s, %s)", (code, name))
        conn.commit()
        return cur.lastrowid


def get_or_create_character(conn, game_id: int, name: str, server: str | None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM characters WHERE game_id=%s AND name=%s AND (server=%s OR (server IS NULL AND %s IS NULL))",
            (game_id, name, server, server),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(
            "INSERT INTO characters (game_id, name, server) VALUES (%s, %s, %s)",
            (game_id, name, server),
        )
        conn.commit()
        return cur.lastrowid


def get_or_create_log_source(conn, game_id: int, character_id: int, file_path: str, live: bool = False) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM log_sources WHERE file_path=%s", (file_path,))
        row = cur.fetchone()
        if row:
            return row
        cur.execute(
            # last_rotated_at starts at creation time, not NULL/epoch -- so a
            # brand-new log file doesn't immediately get treated as
            # "overdue" and rotated away seconds after it starts being
            # tailed (see _most_recent_rotation_boundary in tailer.py).
            "INSERT INTO log_sources (game_id, character_id, file_path, last_byte_offset, live, last_rotated_at) "
            "VALUES (%s, %s, %s, 0, %s, UTC_TIMESTAMP())",
            (game_id, character_id, file_path, live),
        )
        conn.commit()
        cur.execute("SELECT * FROM log_sources WHERE id=%s", (cur.lastrowid,))
        return cur.fetchone()


def update_log_source_offset(conn, log_source_id: int, offset: int):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE log_sources SET last_byte_offset=%s, last_parsed_at=NOW() WHERE id=%s",
            (offset, log_source_id),
        )
    conn.commit()


def get_rotation_settings(conn, game_id: int) -> dict:
    """Returns this game's rotation policy, or a manual-only default if it's
    never been configured -- a game the user hasn't touched shouldn't
    silently start auto-rotating."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM rotation_settings WHERE game_id=%s", (game_id,))
        row = cur.fetchone()
    if row is not None:
        return row
    return {
        "game_id": game_id, "mode": "manual", "day_of_week": None,
        "day_of_month": None, "hour": 9, "size_bytes": None, "manual_trigger_at": None,
    }


def upsert_rotation_settings(
    conn, game_id: int, mode: str, day_of_week: int | None, day_of_month: int | None,
    hour: int, size_bytes: int | None,
):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rotation_settings (game_id, mode, day_of_week, day_of_month, hour, size_bytes) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE mode=VALUES(mode), day_of_week=VALUES(day_of_week), "
            "day_of_month=VALUES(day_of_month), hour=VALUES(hour), size_bytes=VALUES(size_bytes)",
            (game_id, mode, day_of_week, day_of_month, hour, size_bytes),
        )
    conn.commit()


def trigger_rotation_now(conn, game_id: int):
    """Sets/refreshes this game's manual rotation trigger -- honored by
    tail_log_source regardless of its configured mode. Leaves mode/other
    columns alone if a row already exists (ON DUPLICATE KEY only touches
    manual_trigger_at); the mode='manual' in the INSERT list only applies
    the first time a game gets a row at all."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rotation_settings (game_id, mode, manual_trigger_at) "
            "VALUES (%s, 'manual', UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE manual_trigger_at=UTC_TIMESTAMP()",
            (game_id,),
        )
    conn.commit()


def mark_log_source_rotated(conn, log_source_id: int, offset: int):
    """Called right after a log rotation: resets the byte offset for the
    now-empty file and records the rotation in UTC so it survives a tailer
    restart -- see tailer.py's _rotation_due, which compares this against
    the game's configured rotation policy (or a manual trigger) to decide
    whether a rotation is overdue, catching up even after the tailer was off
    for days."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE log_sources SET last_byte_offset=%s, last_parsed_at=NOW(), last_rotated_at=UTC_TIMESTAMP() "
            "WHERE id=%s",
            (offset, log_source_id),
        )
    conn.commit()
