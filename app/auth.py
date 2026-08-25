"""Доступ по персональной ссылке.

Нетехническому человеку нельзя выдать «токен в заголовке Authorization» —
он получит ссылку вида https://host/?k=xxxx, откроет её один раз, и дальше
браузер помнит доступ. Внутри это тот же bearer-токен.

Список людей задаётся переменной USERS ("login:Имя Фамилия" через запятую),
токены генерируются один раз и живут в volume рядом с базой.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from typing import Any

from .config import settings


def _parse_roster() -> list[tuple[str, str]]:
    raw = os.environ.get("USERS", "").strip()
    roster: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        login, _, display = chunk.partition(":")
        login = login.strip()
        if login:
            roster.append((login, display.strip() or login))
    return roster


def load_users() -> dict[str, dict[str, Any]]:
    """Возвращает {login: {token, display_name, role}}, дописывая новых из USERS."""
    users: dict[str, dict[str, Any]] = {}
    if settings.users_path.exists():
        users = json.loads(settings.users_path.read_text(encoding="utf-8"))

    changed = False
    for login, display in _parse_roster():
        if login not in users:
            users[login] = {"token": secrets.token_urlsafe(32), "role": "user"}
            changed = True
        if users[login].get("display_name") != display:
            users[login]["display_name"] = display
            changed = True

    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    admin = users.get("admin")
    if admin is None:
        users["admin"] = {
            "token": admin_token or secrets.token_urlsafe(32),
            "display_name": "Администратор",
            "role": "admin",
        }
        changed = True
    elif admin_token and admin.get("token") != admin_token:
        admin["token"] = admin_token
        changed = True

    if changed:
        settings.users_path.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            settings.users_path.chmod(0o600)
        except OSError:
            pass
    return users


USERS: dict[str, dict[str, Any]] = load_users()


def resolve(token: str | None) -> tuple[str, dict[str, Any]] | None:
    """Токен → (login, профиль). Сравнение постоянного времени."""
    if not token:
        return None
    for login, profile in USERS.items():
        if hmac.compare_digest(profile.get("token", ""), token):
            return login, profile
    return None


def is_admin(profile: dict[str, Any]) -> bool:
    return profile.get("role") == "admin"


def display_name(login: str) -> str:
    return USERS.get(login, {}).get("display_name", login)


def access_links(base_url: str) -> list[dict[str, str]]:
    base = base_url.rstrip("/")
    return [
        {
            "login": login,
            "display_name": profile.get("display_name", login),
            "role": profile.get("role", "user"),
            "link": f"{base}/?k={profile.get('token', '')}",
        }
        for login, profile in USERS.items()
    ]
