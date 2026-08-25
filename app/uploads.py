"""Картинки к заявкам.

Нетехническому человеку проще показать скриншот, чем описать словами
«шапка съехала». Файл сначала попадает в личный черновик пользователя,
а при отправке заявки переезжает в её папку и уходит в Codex флагом `-i`.

Имя файла всегда генерируем сами: то, что пришло из браузера, в файловую
систему не попадает никогда.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from .config import settings

MAX_BYTES = 8 * 1024 * 1024
MAX_PER_MESSAGE = 4
STAGING_TTL = 24 * 3600  # черновики, о которых забыли, живут сутки

SAFE_NAME = re.compile(r"^[0-9a-f]{24}\.(png|jpg|gif|webp)$")

CONTENT_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


class UploadError(ValueError):
    pass


def sniff(data: bytes) -> str:
    """Тип определяем по содержимому: заголовку Content-Type верить нельзя."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise UploadError("Это не картинка. Подойдут PNG, JPEG, GIF или WebP.")


def _uploads_root() -> Path:
    return settings.data_dir / "uploads"


def staging_dir(user: str) -> Path:
    path = _uploads_root() / "staging" / re.sub(r"[^A-Za-z0-9_-]", "_", user)
    path.mkdir(parents=True, exist_ok=True)
    return path


def request_dir(request_id: int) -> Path:
    path = _uploads_root() / f"req-{request_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_staged(user: str, data: bytes) -> str:
    if not data:
        raise UploadError("Пустой файл")
    if len(data) > MAX_BYTES:
        raise UploadError(f"Картинка больше {MAX_BYTES // (1024 * 1024)} МБ")
    kind = sniff(data)
    name = f"{secrets.token_hex(12)}.{kind}"
    target = staging_dir(user) / name
    target.write_bytes(data)
    # Агент работает под другим пользователем и читает файл по группе.
    target.chmod(0o640)
    return name


def staged_path(user: str, name: str) -> Path | None:
    if not SAFE_NAME.match(name):
        return None
    path = staging_dir(user) / name
    return path if path.is_file() else None


def attached_path(request_id: int, name: str) -> Path | None:
    if not SAFE_NAME.match(name):
        return None
    path = request_dir(request_id) / name
    return path if path.is_file() else None


def attach(request_id: int, user: str, names: list[str]) -> list[str]:
    """Переносит черновики в папку заявки. Возвращает принятые имена."""
    if len(names) > MAX_PER_MESSAGE:
        raise UploadError(f"К одному сообщению можно приложить не больше {MAX_PER_MESSAGE} картинок")
    accepted: list[str] = []
    for name in names:
        source = staged_path(user, name)
        if not source:
            continue
        target = request_dir(request_id) / name
        shutil.move(str(source), str(target))
        target.chmod(0o640)
        accepted.append(name)
    return accepted


def clone(source_id: int, target_id: int, names: list[str]) -> list[str]:
    """Повтор заявки забирает картинки с собой — человек прикладывал их не зря."""
    accepted: list[str] = []
    for name in names:
        source = attached_path(source_id, name)
        if not source:
            continue
        target = request_dir(target_id) / name
        shutil.copyfile(source, target)
        target.chmod(0o640)
        accepted.append(name)
    return accepted


def paths_for(request_id: int, names: list[str]) -> list[Path]:
    out: list[Path] = []
    for name in names:
        path = attached_path(request_id, name)
        if path:
            out.append(path)
    return out


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(name.rsplit(".", 1)[-1], "application/octet-stream")


def cleanup_request(request_id: int) -> None:
    shutil.rmtree(request_dir(request_id), ignore_errors=True)


def sweep_staging() -> None:
    """Забытые черновики не должны копиться в volume."""
    root = _uploads_root() / "staging"
    if not root.exists():
        return
    deadline = time.time() - STAGING_TTL
    for path in root.glob("*/*"):
        try:
            if path.is_file() and path.stat().st_mtime < deadline:
                path.unlink()
        except OSError:
            pass
