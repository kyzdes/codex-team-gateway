"""Доступ по персональной ссылке.

Нетехническому человеку нельзя выдать «токен в заголовке Authorization» —
он получит ссылку вида https://host/?k=xxxx, откроет её один раз, и дальше
браузер помнит доступ. Внутри это тот же bearer-токен.

Источник правды — таблица people в базе. Раньше люди лежали в users.json, и
чтобы завести сотрудника или закрыть ему доступ, приходилось править
переменные окружения и перезапускать инстанс. Теперь это делает администратор
из интерфейса, и изменение действует сразу. Переменная USERS осталась только
затравкой первого запуска, а старый users.json импортируется один раз —
вместе с токенами, иначе у людей отвалились бы уже выданные ссылки.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
from typing import Any

from . import db
from .config import settings

log = logging.getLogger(__name__)

ADMIN_LOGIN = "admin"

# Логин администратор вводит руками, а дальше он попадает в имена папок с
# картинками и в служебные подписи, поэтому пускаем только безопасный ASCII.
LOGIN_RE = re.compile(r"^[a-z0-9_-]{2,32}$")

MAX_NAME_LENGTH = 80


class PeopleError(ValueError):
    """Ошибка управления людьми. Текст показываем администратору как есть."""


# Кэш людей: resolve() дёргается на каждый запрос, включая каждое событие
# ленты, и ходить за этим в SQLite незачем. Любая правка обязана звать
# invalidate() — иначе отключённый человек продолжил бы ходить по старой
# ссылке до перезапуска процесса.
_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None
_seeded = False


# --- внутреннее -----------------------------------------------------------

def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _parse_roster() -> list[tuple[str, str]]:
    """USERS="login:Имя, login2:Имя2" → пары (логин, имя).

    Логин берём дословно: под ним в базе уже могут лежать заявки, и любая
    «нормализация» оторвала бы человека от его истории.
    """
    roster: list[tuple[str, str]] = []
    for chunk in os.environ.get("USERS", "").split(","):
        login, _, display = chunk.strip().partition(":")
        login = login.strip()
        if login:
            roster.append((login, display.strip() or login))
    return roster


def _profile(row: dict[str, Any]) -> dict[str, Any]:
    """Строка people → профиль: disabled в SQLite целое, наружу нужен bool."""
    login = str(row.get("login") or "")
    return {
        "login": login,
        "display_name": str(row.get("display_name") or login),
        "token": str(row.get("token") or ""),
        "role": str(row.get("role") or "user"),
        "disabled": bool(row.get("disabled")),
        "created_at": str(row.get("created_at") or ""),
    }


def _people() -> dict[str, dict[str, Any]]:
    """Все люди, включая отключённых: их имена нужны в старых заявках."""
    global _cache
    with _lock:
        if _cache is None:
            if not _seeded:
                seed_people()
            _cache = {}
            for row in db.list_people(include_disabled=True):
                person = _profile(row)
                if person["login"]:
                    _cache[person["login"]] = person
        return _cache


def _require(login: str) -> dict[str, Any]:
    """Профиль по логину или понятная ошибка — с этого начинается любая правка."""
    person = _people().get((login or "").strip())
    if person is None:
        raise PeopleError(f"Человек «{login}» не найден")
    return person


def _import_legacy_file() -> None:
    """Разовый перенос users.json в базу.

    Токены переносим как есть: у людей на руках ссылки, и новый токен означал
    бы «извините, ваш доступ сломался». Логины — тоже дословно, под ними в
    базе лежат заявки. После переноса файл переименовываем, чтобы импорт не
    повторялся и чтобы по volume было видно, что источник правды сменился.
    """
    path = settings.users_path
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("users.json не прочитан (%s), импорт пропущен", exc)
        return
    if not isinstance(raw, dict):
        log.warning("users.json не похож на список людей, импорт пропущен")
        return

    for login, profile in raw.items():
        if not login or not isinstance(profile, dict) or db.get_person(login) is not None:
            continue
        db.upsert_person(
            login,
            str(profile.get("display_name") or login),
            str(profile.get("token") or "") or _new_token(),
            "admin" if profile.get("role") == "admin" else "user",
        )

    imported = path.with_name(path.name + ".imported")
    try:
        path.replace(imported)
        # В файле лежат токены доступа как есть, а прежние версии клали его
        # режимом 0644 — то есть пользователь агента прочитал бы их прямо из
        # тома. Резервную копию оставляем, но закрываем.
        imported.chmod(0o600)
    except OSError as exc:
        log.warning("users.json не переименован (%s), импорт повторится вхолостую", exc)


def _seed_admin() -> None:
    """ADMIN_TOKEN — единственный вход в админку на свежем инстансе, поэтому
    он побеждает то, что лежит в базе. Обновляем только токен: имя, роль и
    признак отключения — это состояние, заданное из интерфейса.
    """
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    person = db.get_person(ADMIN_LOGIN)
    if person is None:
        issued = token or _new_token()
        db.upsert_person(ADMIN_LOGIN, "Администратор", issued, "admin")
        if not token:
            # Сгенерированный токен больше нигде не появится: users.json мы не
            # пишем, а в админку без него не войти — то есть свежий инстанс
            # запирался бы наглухо. Печатаем ровно один раз, при заведении.
            log.warning(
                "ADMIN_TOKEN не задан, выдан разовый токен администратора. "
                "Сохраните ссылку /?k=%s — другого способа попасть в админку нет.",
                issued,
            )
    elif token and person.get("token") != token:
        db.set_person_token(ADMIN_LOGIN, token)


# --- публичное API --------------------------------------------------------

def invalidate() -> None:
    """Сбросить кэш людей. Обязательна после любой правки таблицы people."""
    global _cache
    with _lock:
        _cache = None


def seed_people() -> None:
    """Затравка таблицы при старте: разовый импорт users.json, затем логины из
    USERS, которых ещё нет.

    Существующие записи не трогаем: иначе перезапуск воскрешал бы отключённого
    человека и подменял бы людям токены. Вызывается лениво при первом
    обращении, так что main.py может дёрнуть её на старте явно, а может и не
    дёргать — повторный вызов ничего не ломает.
    """
    global _seeded
    with _lock:
        _import_legacy_file()
        for login, display in _parse_roster():
            if db.get_person(login) is None:
                db.upsert_person(login, display, _new_token(), "user")
        _seed_admin()
        _seeded = True
        invalidate()
        # Людей и их токены нужно немедленно уложить в основной файл базы:
        # см. db.checkpoint().
        db.checkpoint()


def resolve(token: str | None) -> tuple[str, dict[str, Any]] | None:
    """Токен → (login, профиль). Сравнение постоянного времени.

    Отключённый человек не проходит: ссылку у него никто не отбирал, она
    просто перестаёт открывать дверь.
    """
    # compare_digest на строках работает только с ASCII, а в ?k= может
    # приехать что угодно — такой токен всё равно не наш.
    if not token or not token.isascii():
        return None
    for login, profile in _people().items():
        if profile["disabled"] or not profile["token"]:
            continue
        if hmac.compare_digest(profile["token"], token):
            return login, dict(profile)
    return None


def is_admin(profile: dict[str, Any]) -> bool:
    return profile.get("role") == "admin"


def display_name(login: str) -> str:
    """Имя для ленты и заголовков.

    Отключённые тоже находятся: их заявки никуда не делись и должны остаться
    подписанными по-человечески, а не логином.
    """
    person = _people().get(login)
    return person["display_name"] if person else login


def _link_row(base: str, person: dict[str, Any]) -> dict[str, Any]:
    """Строка списка людей. Токен наружу отдаём только внутри ссылки: админу
    нужна именно она, а отдельное поле кто-нибудь однажды покажет на экране."""
    return {
        "login": person["login"],
        "display_name": person["display_name"],
        "role": person["role"],
        "disabled": person["disabled"],
        "link": f"{base}/?k={person['token']}",
    }


def access_links(base_url: str) -> list[dict[str, Any]]:
    """Персональные ссылки для админки — вместе с признаком отключения."""
    base = base_url.rstrip("/")
    return [_link_row(base, person) for person in _people().values()]


def access_link(base_url: str, login: str) -> dict[str, Any]:
    """Одна строка того же списка: ответ на добавление, отключение и ротацию.

    Формат общий с access_links намеренно — интерфейс правит список людей на
    месте, и строка после действия обязана быть такой же, как пришедшая в нём.
    """
    return _link_row(base_url.rstrip("/"), _require(login))


def add_person(login: str, display_name: str) -> dict[str, Any]:
    """Завести человека из админки.

    Возвращает профиль вместе с токеном: администратору нужно тут же скопировать
    ссылку и отдать её сотруднику.
    """
    login = (login or "").strip().lower()
    if not LOGIN_RE.fullmatch(login):
        raise PeopleError(
            "Логин: латинские буквы в нижнем регистре, цифры, дефис или подчёркивание, от 2 до 32 символов"
        )
    name = " ".join((display_name or "").split())
    if not name:
        raise PeopleError("Впишите имя — его человек увидит в интерфейсе")
    if len(name) > MAX_NAME_LENGTH:
        raise PeopleError(f"Имя длиннее {MAX_NAME_LENGTH} символов")
    if db.get_person(login) is not None:
        raise PeopleError(f"Логин «{login}» уже занят")

    db.upsert_person(login, name, _new_token(), "user")
    invalidate()
    return dict(_require(login))


def disable_person(login: str) -> None:
    """Закрыть доступ, сохранив человека в истории заявок."""
    person = _require(login)
    if person["role"] == "admin":
        raise PeopleError("Администратора отключать нельзя — иначе некому будет вернуть доступ")
    db.set_person_disabled(person["login"], True)
    invalidate()


def enable_person(login: str) -> None:
    """Вернуть доступ. Токен остаётся прежним — старая ссылка снова работает."""
    person = _require(login)
    db.set_person_disabled(person["login"], False)
    invalidate()


def rotate_token(login: str) -> str:
    """Выдать новую ссылку; старая перестаёт работать сразу.

    Этим лечится «ссылку переслали в общий чат». Оговорка про администратора:
    если в окружении задан ADMIN_TOKEN, при следующем старте инстанса он
    вернёт свой токен обратно.
    """
    person = _require(login)
    token = _new_token()
    db.set_person_token(person["login"], token)
    invalidate()
    return token
