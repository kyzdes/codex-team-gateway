"""Хранилище заявок. SQLite из стандартной библиотеки — отдельная БД не нужна."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user         TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    body         TEXT    NOT NULL,
    title        TEXT,
    summary      TEXT,
    user_visible TEXT,
    notes        TEXT,
    risk         TEXT,
    question     TEXT,
    thread_id    TEXT,
    branch       TEXT,
    head_sha     TEXT,
    pr_number    INTEGER,
    pr_url       TEXT,
    checks_status TEXT,
    checks_detail TEXT,
    files        TEXT,
    text_changes TEXT,
    images       TEXT,
    tests_local  TEXT,
    tests_output TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    merged_at    TEXT,
    deployed_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    ts         TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    text       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user, id DESC);
"""

JSON_FIELDS = {"user_visible", "files", "text_changes", "images"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Столбцы, добавленные после первого релиза: CREATE TABLE IF NOT EXISTS их
# не создаст, поэтому досыпаем вручную при старте.
LATER_COLUMNS = {"images": "TEXT"}


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        existing = {row["name"] for row in _conn.execute("PRAGMA table_info(requests)")}
        for column, kind in LATER_COLUMNS.items():
            if column not in existing:
                _conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {kind}")
        _conn.commit()
    return _conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for key in JSON_FIELDS:
        raw = out.get(key)
        out[key] = json.loads(raw) if raw else []
    return out


def create_request(user: str, body: str) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO requests (user, status, body, created_at, updated_at) VALUES (?,?,?,?,?)",
            (user, "queued", body, now(), now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_request(request_id: int, **fields: Any) -> None:
    if not fields:
        return
    payload = {
        k: (json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and not isinstance(v, str) else v)
        for k, v in fields.items()
    }
    payload["updated_at"] = now()
    columns = ", ".join(f"{k} = ?" for k in payload)
    with _lock:
        conn = connect()
        conn.execute(f"UPDATE requests SET {columns} WHERE id = ?", (*payload.values(), request_id))
        conn.commit()


def get_request(request_id: int) -> dict[str, Any] | None:
    with _lock:
        row = connect().execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_requests(user: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM requests"
    args: tuple[Any, ...] = ()
    if user:
        sql += " WHERE user = ?"
        args = (user,)
    sql += " ORDER BY id DESC LIMIT ?"
    with _lock:
        rows = connect().execute(sql, (*args, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


def requests_in_statuses(statuses: Iterable[str]) -> list[dict[str, Any]]:
    statuses = list(statuses)
    marks = ",".join("?" * len(statuses))
    with _lock:
        rows = connect().execute(
            f"SELECT * FROM requests WHERE status IN ({marks}) ORDER BY id", statuses
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def add_event(request_id: int, kind: str, text: str) -> dict[str, Any]:
    ts = now()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO events (request_id, ts, kind, text) VALUES (?,?,?,?)",
            (request_id, ts, kind, text),
        )
        conn.commit()
    return {"request_id": request_id, "ts": ts, "kind": kind, "text": text}


def list_events(request_id: int, limit: int = 300) -> list[dict[str, Any]]:
    with _lock:
        rows = connect().execute(
            "SELECT ts, kind, text FROM events WHERE request_id = ? ORDER BY id LIMIT ?",
            (request_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
