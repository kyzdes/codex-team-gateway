"""Хранилище заявок. SQLite из стандартной библиотеки — отдельная БД не нужна."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import settings

log = logging.getLogger(__name__)

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
    usage        TEXT,
    approved_by  TEXT,
    approved_at  TEXT,
    retried_at   TEXT,
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

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS people (
    login        TEXT PRIMARY KEY,
    display_name TEXT    NOT NULL,
    token        TEXT    NOT NULL,
    role         TEXT    NOT NULL DEFAULT 'user',
    disabled     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_request ON events(request_id);
CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user, id DESC);
"""

JSON_FIELDS = {"user_visible", "files", "text_changes", "images", "usage"}
# Пустое значение обязано совпадать по типу с заполненным, иначе интерфейс
# сломается на первой же заявке без расхода: usage — объект, остальное — списки.
DICT_JSON_FIELDS = {"usage"}
# Что именно складываем в usage. Всё, чего Codex не прислал, считаем нулём.
USAGE_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "turns")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Столбцы, добавленные после первого релиза: CREATE TABLE IF NOT EXISTS их
# не создаст, поэтому досыпаем вручную при старте.
LATER_COLUMNS = {
    "images": "TEXT",
    "usage": "TEXT",
    "approved_by": "TEXT",
    "approved_at": "TEXT",
    "retried_at": "TEXT",
}


def _restrict_permissions() -> None:
    """Закрыть базу и логи от всех, кроме владельца.

    В образе пользователь агента состоит в одной группе со шлюзом, а SQLite и
    open() создают файлы режимом 0644/0664 — то есть по умолчанию агент читал
    бы таблицу people со всеми токенами доступа, включая админский, и переписку
    по чужим заявкам в логах прогонов.

    Про -wal и -shm: SQLite создаёт их по образцу основного файла, но режим
    запоминает в момент ОТКРЫТИЯ соединения. Поэтому chmod обязан случиться
    до connect(), а не между connect() и включением WAL — иначе спутники
    останутся 0644, и в них прекрасно читается свежая транзакция с токенами.
    Проверено: так и было. Спутники дочищаем явно, они могли остаться
    открытыми с прошлого запуска.
    """
    targets = (
        (settings.db_path, 0o600),
        (settings.db_path.with_name(settings.db_path.name + "-wal"), 0o600),
        (settings.db_path.with_name(settings.db_path.name + "-shm"), 0o600),
        (settings.logs_dir, 0o750),
    )
    for path, mode in targets:
        if not path.exists():
            continue
        try:
            os.chmod(path, mode)
        except OSError as exc:
            # Права могут не поддерживаться (примонтированная шара, Windows) —
            # это повод громко предупредить, а не отказаться работать.
            log.warning("Не удалось закрыть доступ к %s: %s", path, exc)


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # Порядок важен: сначала закрываем права, потом открываем соединение —
        # режим спутников -wal/-shm SQLite берёт с базы именно при открытии.
        _restrict_permissions()
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _restrict_permissions()
        _conn.executescript(SCHEMA)
        existing = {row["name"] for row in _conn.execute("PRAGMA table_info(requests)")}
        for column, kind in LATER_COLUMNS.items():
            if column not in existing:
                _conn.execute(f"ALTER TABLE requests ADD COLUMN {column} {kind}")
        _conn.commit()
    return _conn


def checkpoint() -> None:
    """Сбросить WAL в основной файл базы.

    Зовётся после затравки людей: их токены только что записаны, а спутник
    -wal легко потерять при копировании volume или снапшоте — и тогда доступ
    отвалится у всей команды разом, причём восстановить его будет нечем:
    users.json к этому моменту уже импортирован и переименован.
    """
    with _lock:
        connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # Пересекаем с ключами строки, чтобы выборки из отдельных столбцов
    # (например, журнал подтверждений) не обрастали лишними полями.
    out = dict(row)
    for key in JSON_FIELDS & out.keys():
        raw = out.get(key)
        out[key] = json.loads(raw) if raw else ({} if key in DICT_JSON_FIELDS else [])
    return out


def _as_int(value: Any) -> int:
    """Счётчики приходят от внешнего процесса: строка или None вместо числа
    не повод ронять сохранение заявки."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def request_owners(request_ids: list[int]) -> dict[int, str]:
    """id заявки → логин автора. Одним запросом, потому что спрашивают сразу
    про все папки с картинками, которые лежат в томе."""
    if not request_ids:
        return {}
    marks = ",".join("?" * len(request_ids))
    with _lock:
        rows = connect().execute(
            f"SELECT id, user FROM requests WHERE id IN ({marks})", request_ids
        ).fetchall()
    return {int(row["id"]): str(row["user"]) for row in rows}


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


# --- рантайм-настройки ----------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    """Переключатели, которые администратор меняет на ходу.

    Живут в базе, а не в окружении: ради паузы приёма заявок нельзя требовать
    перезапуск инстанса — это простой, а не настройка.
    """
    with _lock:
        row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None or row["value"] is None:
        return default
    return str(row["value"])


def set_setting(key: str, value: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


# --- люди -----------------------------------------------------------------

def _person_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    # В SQLite флаг лежит числом; наружу отдаём bool, чтобы он таким же
    # доехал до интерфейса и никто не сравнивал его с нулём вручную.
    out = dict(row)
    out["disabled"] = bool(out["disabled"])
    return out


def list_people(include_disabled: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM people"
    if not include_disabled:
        sql += " WHERE disabled = 0"
    sql += " ORDER BY login"
    with _lock:
        rows = connect().execute(sql).fetchall()
    return [_person_to_dict(r) for r in rows]


def get_person(login: str) -> dict[str, Any] | None:
    with _lock:
        row = connect().execute("SELECT * FROM people WHERE login = ?", (login,)).fetchone()
    return _person_to_dict(row) if row else None


def upsert_person(login: str, display_name: str, token: str, role: str = "user") -> None:
    """Заводит человека или обновляет ему имя, токен и роль.

    Отключение и дату появления сознательно не трогаем: пересев списка из
    переменной USERS не должен возвращать доступ тому, кого администратор
    отключил руками.
    """
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT INTO people (login, display_name, token, role, disabled, created_at) "
            "VALUES (?,?,?,?,0,?) "
            "ON CONFLICT(login) DO UPDATE SET "
            "display_name = excluded.display_name, token = excluded.token, role = excluded.role",
            (login, display_name, token, role, now()),
        )
        conn.commit()


def set_person_disabled(login: str, disabled: bool) -> None:
    with _lock:
        conn = connect()
        conn.execute("UPDATE people SET disabled = ? WHERE login = ?", (1 if disabled else 0, login))
        conn.commit()


def set_person_token(login: str, token: str) -> None:
    with _lock:
        conn = connect()
        conn.execute("UPDATE people SET token = ? WHERE login = ?", (token, login))
        conn.commit()


# --- расход и журнал ------------------------------------------------------

def count_requests_since(user: str, since_iso: str) -> int:
    """Сколько заявок человек создал начиная с указанного момента.

    Все даты пишутся одним форматом UTC, поэтому сравнение строк здесь
    равносильно сравнению времени.
    """
    with _lock:
        row = connect().execute(
            "SELECT COUNT(*) AS n FROM requests WHERE user = ? AND created_at >= ?",
            (user, since_iso),
        ).fetchone()
    return int(row["n"])


def add_usage(request_id: int, usage: dict[str, Any]) -> None:
    """Прибавляет расход прогона к уже накопленному по заявке.

    Уточняющий вопрос, ответ человека и повторная правка — это несколько
    прогонов одной заявки, и стоят они все.
    """
    with _lock:
        conn = connect()
        row = conn.execute("SELECT usage FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            return
        raw = row["usage"]
        total: dict[str, Any] = json.loads(raw) if raw else {}
        for key in USAGE_KEYS:
            total[key] = _as_int(total.get(key)) + _as_int(usage.get(key))
        conn.execute(
            "UPDATE requests SET usage = ?, updated_at = ? WHERE id = ?",
            (json.dumps(total, ensure_ascii=False), now(), request_id),
        )
        conn.commit()


def usage_totals(days: int) -> list[dict[str, Any]]:
    """Расход по людям за последние N дней, самые дорогие сверху.

    Складываем в Python, а не запросом: usage хранится JSON-объектом, и
    разбирать его средствами SQLite ради десятка строк незачем.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    with _lock:
        rows = connect().execute(
            "SELECT user, usage FROM requests WHERE created_at >= ?", (since,)
        ).fetchall()
    totals: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = totals.setdefault(
            row["user"],
            {"user": row["user"], "requests": 0, "input_tokens": 0, "output_tokens": 0},
        )
        entry["requests"] += 1
        spent = json.loads(row["usage"]) if row["usage"] else {}
        entry["input_tokens"] += _as_int(spent.get("input_tokens"))
        entry["output_tokens"] += _as_int(spent.get("output_tokens"))
    return sorted(totals.values(), key=lambda e: e["input_tokens"] + e["output_tokens"], reverse=True)


def approvals_journal(limit: int = 50) -> list[dict[str, Any]]:
    """Кто и когда отправил правку в прод — это спрашивают задним числом."""
    with _lock:
        rows = connect().execute(
            "SELECT id, title, user, approved_by, approved_at, pr_url, status FROM requests "
            "WHERE approved_at IS NOT NULL AND approved_at != '' "
            "ORDER BY approved_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
