"""Картинки к заявкам.

Нетехническому человеку проще показать скриншот, чем описать словами
«шапка съехала». Файл сначала попадает в личный черновик пользователя,
а при отправке заявки переезжает в её папку и уходит в Codex флагом `-i`.

Имя файла всегда генерируем сами: то, что пришло из браузера, в файловую
систему не попадает никогда.

Место в volume не резиновое, поэтому ограничителей два: личный потолок на
человека (UPLOAD_QUOTA_MB) и вычистка картинок давно закрытых заявок по сроку
хранения (purge_old, RETENTION_DAYS). Потолок считается по всему, что человек
реально занимает, а не по одним черновикам: attach() физически ВЫНОСИТ файлы
из черновиков в папку заявки, и счётчик черновиков обнулялся после каждой
отправки — предел обходился бесконечно, заявка за заявкой.
"""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Iterable

from . import db
from .config import settings

MAX_BYTES = 8 * 1024 * 1024
MAX_PER_MESSAGE = 4
STAGING_TTL = 24 * 3600  # черновики, о которых забыли, живут сутки

SAFE_NAME = re.compile(r"^[0-9a-f]{24}\.(png|jpg|gif|webp)$")
REQUEST_DIR = re.compile(r"^req-(\d+)$")

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


def _dir_bytes(directory: Path) -> int:
    """Сколько занимают файлы каталога. Пропавший каталог — это ноль, а не ошибка."""
    total = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for path in entries:
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            # Файл могли прямо сейчас перенести в заявку — просто не считаем его.
            continue
    return total


def _request_dirs() -> dict[int, Path]:
    """Папки картинок, которые сейчас лежат в томе: id заявки → путь."""
    root = _uploads_root()
    if not root.exists():
        return {}
    found: dict[int, Path] = {}
    for path in root.glob("req-*"):
        match = REQUEST_DIR.match(path.name)
        if match and path.is_dir():
            found[int(match.group(1))] = path
    return found


def user_staging_bytes(user: str) -> int:
    """Сколько занимают загруженные, но ещё не отправленные картинки человека."""
    return _dir_bytes(staging_dir(user))


def user_bytes(user: str) -> int:
    """Сколько человек занимает картинками всего: черновики плюс папки его заявок.

    Считать одни черновики нельзя: отправка заявки переносит файлы в её папку,
    и счётчик обнулялся бы после каждой отправки. Место при этом никуда не
    девается — оно освобождается только уборкой по сроку хранения.
    """
    dirs = _request_dirs()
    owners = db.request_owners(sorted(dirs))
    total = user_staging_bytes(user)
    for request_id, path in dirs.items():
        if owners.get(request_id) == user:
            total += _dir_bytes(path)
    return total


def _check_quota(user: str, incoming: int) -> None:
    """Потолок на человека.

    Без него один увлёкшийся сотрудник забивает общий volume — и тогда
    перестают приниматься заявки у всех, а SQLite получает «disk is full».
    """
    limit = settings.upload_quota_mb * 1024 * 1024
    if limit <= 0:  # 0 = лимит выключен, так удобнее на своём сервере
        return
    used = user_bytes(user)
    if used + incoming <= limit:
        return
    raise UploadError(
        f"Не поместится: ваши картинки уже занимают {_mb(used)} МБ "
        f"из {settings.upload_quota_mb} МБ. Снимки закрытых заявок удаляются "
        f"сами через {settings.retention_days} дн."
    )


def _mb(value: int) -> str:
    """Человеку понятнее «12,5», чем 13107200 байт."""
    return f"{value / 1024 / 1024:.1f}".replace(".", ",")


def save_staged(user: str, data: bytes) -> str:
    if not data:
        raise UploadError("Пустой файл")
    if len(data) > MAX_BYTES:
        raise UploadError(f"Картинка больше {MAX_BYTES // (1024 * 1024)} МБ")
    kind = sniff(data)
    # Сначала разбираемся с самим файлом: если человек прислал не картинку,
    # честнее сказать про это, чем валить всё на переполненный запас.
    _check_quota(user, len(data))
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


def purge_old(days: int, keep: Iterable[int] = ()) -> int:
    """Ретеншен картинок: убрать папки заявок, которых давно никто не касался.

    Незавершённые заявки не трогаем ни при каком возрасте: заявка может неделю
    ждать ответа человека, а после ответа прогон пойдёт заново — и пойдёт он со
    скриншотом, ради которого заявку и завели. Одной метки времени для этого
    мало, поэтому список живых заявок приходит снаружи, из пайплайна.

    У тех, чьи файлы всё-таки удалили, чистим список картинок в базе: иначе в
    карточке остались бы ссылки на несуществующие файлы.

    Возвращает число вычищенных заявок (папок), а не файлов.
    """
    if days <= 0:
        return 0
    protected = set(keep)
    deadline = time.time() - days * 86400
    removed = 0
    for request_id, path in _request_dirs().items():
        if request_id in protected or _last_touch(path) >= deadline:
            continue
        shutil.rmtree(path, ignore_errors=True)
        db.update_request(request_id, images=[])
        removed += 1
    return removed


def _last_touch(directory: Path) -> float:
    """Самая свежая метка среди самой папки и её содержимого."""
    try:
        latest = directory.stat().st_mtime
        for item in directory.iterdir():
            latest = max(latest, item.stat().st_mtime)
    except OSError:
        # Папку разбирают прямо сейчас — считаем свежей и не трогаем.
        return time.time()
    return latest
