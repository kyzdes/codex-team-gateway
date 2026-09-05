"""Работа с git: зеркало проекта, worktree на заявку, факты об изменениях, пуш.

Каждая заявка = своя ветка от актуального origin/<base> и своя папка-worktree.
Так две заявки не перемешиваются между собой, а мерж одной не тащит за собой
недоделанную вторую.

Границу доверия здесь держать так же строго, как в остальном шлюзе. Рабочая
копия заявки — территория агента: он правит там файлы, .gitattributes и даже
сам указатель `.git`. Каталог /data/repo — территория шлюза: его конфиг и
крючки закрыты от группы, и только там выполняются команды с токеном.

Причина именно такая, а не «на всякий случай»: конфиг репозитория — это
исполняемый код и адрес назначения одновременно. `core.hooksPath`,
`diff.<драйвер>.textconv`, `filter.<драйвер>.clean` и `credential.helper`
запускают чужую команду от имени того, кто позвал git, а `url.<чужой>.insteadOf`
уводит пуш на посторонний хост вместе с заголовком авторизации. Поэтому:
  * клон, fetch и push идут только в /data/repo;
  * факты о правке считаются там же — по ветке, а не по рабочей копии;
  * всё, что лезет в рабочую копию заявки, выполняется от имени агента.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import codex_runner
from .config import github_token, settings

logger = logging.getLogger(__name__)

COMMIT_NAME = "Codex (заявка)"
COMMIT_EMAIL = "codex@localhost"

# Файлы, из которых имеет смысл показывать текстовую разницу человеку.
TEXTY = re.compile(r"\.(html?|jinja2?|j2|twig|vue|svelte|jsx|tsx|md|txt|ya?ml|json|po|csv|py|js|ts|css)$", re.I)
CODE_NOISE = re.compile(r"^\s*(import|from|def |class |return|const |let |var |@|#|//|/\*|\*|\}|\{|\)|\()")

# Клон и любая правка конфига копии — строго по одному за раз. Позвать
# ensure_repo() могут одновременно прогрев после сохранения ключа, первая
# заявка и вторая параллельная: второй клон видел бы непустой каталог и падал
# бы «destination path already exists» — текстом, который сотрудник читает у
# себя в карточке.
_REPO_LOCK = asyncio.Lock()

# Конфиг, который шлюз навязывает поверх любого другого на время своей команды.
# GIT_CONFIG_* читаются последними, поэтому это работает, даже если в конфиг
# репозитория всё-таки кто-то дописал своё. Всё перечисленное — способы
# заставить git выполнить постороннюю команду от имени того, кто его запустил.
HARDENING: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", ""),
    ("credential.helper", ""),  # пустое значение сбрасывает весь список помощников
    ("uploadPack.packObjectsHook", ""),
)

# Разница, посчитанная без драйверов из конфига и .gitattributes: даже если
# такой драйвер туда попадёт, команда его не выполнит.
DIFF_SAFE: tuple[str, ...] = ("--no-textconv", "--no-ext-diff")

# Что в локальной копии закрыто от группы (то есть от пользователя агента).
# Конфиг, крючки, info и список worktree дают исполнение команд от имени
# шлюза, поэтому группе там делать нечего. Объекты, ссылки и логи, наоборот,
# обязаны оставаться доступными на запись: без них агент не закоммитит.
REPO_LOCKED_MODES: tuple[tuple[str, int], ...] = (
    ("", 0o755),  # сам каталог копии: иначе .git можно просто подменить целиком
    (".git", 0o755),
    (".git/config", 0o640),
    (".git/hooks", 0o755),
    (".git/info", 0o755),
    (".git/worktrees", 0o755),
)

# Ключи, которые git заводит в конфиге копии сам. Всё остальное там — чужая
# рука: раньше файл был доступен группе на запись, поэтому одних новых прав
# мало, дописанное до них нужно снять.
ALLOWED_CONFIG: frozenset[str] = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "core.ignorecase",
        "core.precomposeunicode",
        "core.sharedrepository",
        "extensions.objectformat",
        "extensions.compatobjectformat",
        "remote.origin.url",
        "remote.origin.fetch",
        "user.name",
        "user.email",
        "gc.auto",
    }
)
# Их заводит `worktree add` на каждую заявку, поэтому перечислить нельзя.
ALLOWED_CONFIG_BRANCH = re.compile(r"^branch\..+\.(remote|merge|rebase|description)$")


class GitError(RuntimeError):
    pass


@dataclass
class Facts:
    """То, что шлюз знает про изменения сам, не веря агенту на слово."""

    commits: int = 0
    head_sha: str = ""
    files: list[dict[str, str]] = field(default_factory=list)
    text_changes: list[dict[str, str]] = field(default_factory=list)


def _auth_env(token: str) -> dict[str, str] | None:
    """Учётка для одной команды — через окружение, а не через аргументы.

    Раньше токен уезжал в argv (`git -c http.extraHeader=...`), а /proc/<pid>/cmdline
    на Linux читает любой пользователь — то есть пользователь агента доставал
    оттуда GitHub-токен шлюза и мог пушить в репозиторий клиента мимо PR и мимо
    кнопки человека. Окружение процесса закрыто по uid, поэтому GIT_CONFIG_*
    (git ≥ 2.31) не видно никому, кроме самого шлюза.

    Токен приходит аргументом: вызывающий читает его один раз на команду,
    чтобы маскировать в ошибке ровно то значение, которым и ходили в GitHub.
    """
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _harden(env: dict[str, str]) -> dict[str, str]:
    """Дописать в GIT_CONFIG_* глушилки исполняемых крючков.

    Продолжаем нумерацию, а не начинаем заново: заголовок с токеном уже мог
    занять нулевой слот, и затереть его значило бы ходить в GitHub без учётки.
    """
    start = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate(HARDENING):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(HARDENING))
    return env


async def git(
    *args: str,
    cwd: Path | None = None,
    auth: bool = False,
    check: bool = True,
    as_agent: bool = False,
) -> str:
    """Одна команда git.

    `auth` — добавить заголовок с токеном; допустимо только в копии шлюза.
    `as_agent` — выполнить от имени пользователя агента. Так запускается всё,
    что работает в рабочей копии заявки: там и файлы, и указатель `.git`, и
    .gitattributes правит агент, а git по ним умеет выполнять команды.
    """
    if auth and as_agent:
        # Ни при каких условиях: токен не должен оказаться в процессе агента.
        raise GitError("внутренняя ошибка: команда с токеном не выполняется от имени агента")
    # Токен берём один раз на команду: администратор может сменить его между
    # вызовами, а маскировать в тексте ошибки нужно именно то значение,
    # с которым команда и работала.
    token = github_token()
    if as_agent:
        argv = codex_runner.agent_command(["git", *args])
        env = codex_runner.clean_env()
    else:
        argv = ["git", *args]
        extra = _auth_env(token) if auth else None
        # GIT_TERMINAL_PROMPT=0: без него git на отказе в доступе пытается
        # спросить логин и висит до таймаута заявки вместо внятной ошибки.
        env = _harden({**os.environ, "GIT_TERMINAL_PROMPT": "0", **(extra or {})})
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        # Каталог репозитория ещё не создан (первый запуск, клон не прошёл).
        raise GitError(f"нет рабочего каталога {cwd}: {exc}") from exc
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        message = err.decode("utf-8", errors="replace").strip() or stdout.strip()
        # Никогда не отдаём наружу строку с токеном.
        if token:
            message = message.replace(token, "***")
        raise GitError(f"git {' '.join(args[:3])}: {message}")
    return stdout


def _lock_repo() -> None:
    """Закрыть от пользователя агента всё, чем можно одолжить личность шлюза.

    Права важнее, чем кажется: агент и шлюз состоят в одной группе, а umask 002
    делает созданное шлюзом доступным группе на запись. С таким конфигом агенту
    хватало одной строчки `insteadOf`, чтобы следующий push шлюза ушёл на его
    хост вместе с заголовком `Authorization`, и одной строчки `textconv`,
    чтобы `git diff` шлюза выполнил его команду.

    Зовём после клона и после каждого `worktree add`: и то и другое заводит
    каталоги заново, уже с групповой записью.
    """
    for name, mode in REPO_LOCKED_MODES:
        path = settings.repo_dir / name if name else settings.repo_dir
        if not path.exists():
            continue
        try:
            os.chmod(path, mode)
        except OSError as exc:
            # Права могут не поддерживаться (примонтированная шара, Windows) —
            # это повод громко предупредить, а не отказаться работать.
            logger.warning("Не удалось закрыть доступ к %s: %s", path, exc)
    hooks = settings.repo_dir / ".git" / "hooks"
    if not hooks.is_dir():
        return
    for entry in hooks.iterdir():
        # Клон кладёт сюда только образцы (*.sample), которые git не запускает.
        # Но если настоящий крючок всё-таки появится, он обязан быть закрыт:
        # его выполнит любая команда git, в том числе наша, с токеном.
        try:
            if entry.is_file():
                os.chmod(entry, 0o700)
        except OSError as exc:
            logger.warning("Не удалось закрыть доступ к %s: %s", entry, exc)


async def _scrub_config() -> None:
    """Выкинуть из конфига копии всё, чего git там не заводил.

    Одних прав мало: файл долго был доступен группе на запись, и дописанное в
    него переживёт любой chmod. Список разрешённого — белый, а не чёрный,
    потому что опасны не конкретные ключи, а сама возможность назвать команду.
    """
    listing = await git(
        "config", "--local", "--list", "--name-only", "-z", cwd=settings.repo_dir, check=False
    )
    for key in filter(None, listing.split("\0")):
        if key in ALLOWED_CONFIG or ALLOWED_CONFIG_BRANCH.match(key):
            continue
        logger.warning("Убираю посторонний ключ из конфига копии проекта: %s", key)
        await git("config", "--local", "--unset-all", key, cwd=settings.repo_dir, check=False)


async def _fetch() -> None:
    """Обновление копии. Без замка: зовут изнутри уже занятой секции."""
    await git("fetch", "origin", "--prune", cwd=settings.repo_dir, auth=True)


async def ensure_repo() -> None:
    """Клонирует проект при первом запуске, дальше просто обновляет.

    Целиком под замком: клон приватного репозитория занимает минуты, и за это
    время сюда обязательно придёт кто-то ещё — прогрев после сохранения ключа,
    первая заявка, вторая параллельная. Раньше второй заходящий видел непустой
    каталог без .git и падал «destination path already exists».
    """
    async with _REPO_LOCK:
        if not (settings.repo_dir / ".git").exists():
            if settings.repo_dir.exists():
                # Обломки прерванного клона: git откажется класть копию в
                # непустой каталог, и заявка встанет намертво до ручной уборки.
                shutil.rmtree(settings.repo_dir, ignore_errors=True)
            settings.repo_dir.parent.mkdir(parents=True, exist_ok=True)
            await git("clone", settings.clone_url, str(settings.repo_dir), auth=True)
        _lock_repo()
        await _scrub_config()
        # Адрес origin — это то, куда уедет ветка вместе с заголовком
        # авторизации, поэтому задаём его сами, а не верим содержимому файла.
        await git("config", "--local", "remote.origin.url", settings.clone_url, cwd=settings.repo_dir)
        await git("config", "--local", "user.name", COMMIT_NAME, cwd=settings.repo_dir)
        await git("config", "--local", "user.email", COMMIT_EMAIL, cwd=settings.repo_dir)
        # Автосборку мусора запускает git агента после его же коммита, а лезет
        # она туда, куда агенту больше нельзя, — пусть не запускается вовсе.
        await git("config", "--local", "gc.auto", "0", cwd=settings.repo_dir)
        await _fetch()


def branch_name(request_id: int) -> str:
    return f"codex/req-{request_id}"


def worktree_path(request_id: int) -> Path:
    return settings.worktrees_dir / f"req-{request_id}"


async def create_worktree(request_id: int) -> tuple[Path, str]:
    """Свежая рабочая копия заявки от актуального origin/<base>.

    Под тем же замком, что и клон: `worktree add` пишет в общий конфиг копии
    (branch.<ветка>.remote), и два одновременных запуска ловят на нём
    «could not lock config file».
    """
    async with _REPO_LOCK:
        await _fetch()
        path = worktree_path(request_id)
        branch = branch_name(request_id)
        if path.exists():
            await _drop_worktree(request_id)
        # Ветка могла остаться от прошлой попытки — снимаем её, чтобы начать с чистого.
        await git("branch", "-D", branch, cwd=settings.repo_dir, check=False)
        await git(
            "worktree", "add", "-b", branch, str(path), f"origin/{settings.base_branch}",
            cwd=settings.repo_dir,
        )
        # Имя и почта коммитов приходят из общего конфига копии (его задаёт
        # ensure_repo), поэтому отдельно в рабочей копии их не ставим: этот
        # `git config` всё равно писал бы в тот же файл, только из каталога,
        # который правит агент.
        #
        # `worktree add` только что завёл .git/worktrees — закрываем каталог от
        # агента: в нём лежит commondir, а он указывает git, откуда брать конфиг.
        _lock_repo()
    return path, branch


async def _drop_worktree(request_id: int) -> None:
    """Снять рабочую копию заявки. Без замка: зовут изнутри занятой секции."""
    path = worktree_path(request_id)
    if not (settings.repo_dir / ".git").exists():
        shutil.rmtree(path, ignore_errors=True)
        return
    await git("worktree", "remove", "--force", str(path), cwd=settings.repo_dir, check=False)
    # git мог отказаться снять копию (каталог держит недобитый процесс агента).
    # Оставленный каталог потом мешает создать worktree заново, поэтому добиваем.
    shutil.rmtree(path, ignore_errors=True)
    await git("worktree", "prune", cwd=settings.repo_dir, check=False)
    # Локальная ветка своё отработала: нужное уже уехало в origin, ненужное
    # не понадобится — иначе они копятся в репозитории по одной на заявку.
    await git("branch", "-D", branch_name(request_id), cwd=settings.repo_dir, check=False)


async def remove_worktree(request_id: int) -> None:
    """Снять рабочую копию заявки. Зовётся на каждом финале заявки, поэтому
    обязана быть безобидной: worktree могло не быть вовсе, а репозитория —
    ещё не появиться (первый запуск, клон не прошёл)."""
    async with _REPO_LOCK:
        await _drop_worktree(request_id)


async def has_uncommitted(path: Path) -> bool:
    return bool((await git("status", "--porcelain", cwd=path, as_agent=True)).strip())


async def commit_all(path: Path, message: str) -> None:
    """Подобрать за агентом то, что он забыл закоммитить.

    От имени агента, а не шлюза: в рабочей копии он правит и файлы, и
    .gitattributes, и указатель `.git`, а `git add`/`commit` по ним выполняют
    чужие команды (clean-фильтры, крючки). Пусть выполняются под тем же
    пользователем, что их и написал.
    """
    await git("add", "-A", cwd=path, as_agent=True)
    await git("commit", "-m", message, cwd=path, as_agent=True)


async def collect_facts(branch: str) -> Facts:
    """Что реально изменилось — по данным git, а не по словам агента.

    Считаем в копии шлюза по ветке заявки, а не в рабочей копии. `git diff`
    исполняет драйверы из конфига репозитория, а конфиг рабочей копии заявки
    агент может подменить целиком — вместе с тем, откуда git его берёт.
    Коммиты агента ветку уже двигают, так что смотреть на неё и достаточно, и
    безопасно: в /data/repo конфиг закрыт.
    """
    base = f"origin/{settings.base_branch}"
    ref = f"refs/heads/{branch}"
    facts = Facts()

    count = (await git("rev-list", "--count", f"{base}..{ref}", cwd=settings.repo_dir)).strip()
    facts.commits = int(count or 0)
    if facts.commits == 0:
        return facts

    facts.head_sha = (await git("rev-parse", ref, cwd=settings.repo_dir)).strip()

    named = await git("diff", *DIFF_SAFE, "--name-status", f"{base}...{ref}", cwd=settings.repo_dir)
    for line in named.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            facts.files.append({"status": parts[0][:1], "path": parts[-1]})

    facts.text_changes = _extract_text_changes(
        await git("diff", *DIFF_SAFE, "-U0", f"{base}...{ref}", cwd=settings.repo_dir)
    )
    return facts


def _is_meaningful_text(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < 3 or len(stripped) > 300:
        return False
    if not re.search(r"[A-Za-zА-Яа-яЁё]{3}", stripped):
        return False
    return not CODE_NOISE.match(stripped)


def _extract_text_changes(diff: str, limit: int = 25) -> list[dict[str, str]]:
    """Грубое «было → стало» по видимым строкам: для проверки правки глазами."""
    changes: list[dict[str, str]] = []
    current_file = ""
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        nonlocal removed, added
        for i in range(max(len(removed), len(added))):
            before = removed[i] if i < len(removed) else ""
            after = added[i] if i < len(added) else ""
            if before == after:
                continue
            if _is_meaningful_text(before) or _is_meaningful_text(after):
                changes.append({"file": current_file, "before": before.strip(), "after": after.strip()})
        removed, added = [], []

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            flush()
            current_file = line[6:]
            continue
        if line.startswith(("--- ", "diff --git", "index ", "new file", "deleted file", "similarity")):
            continue
        if line.startswith("@@"):
            flush()
            continue
        if not current_file or not TEXTY.search(current_file):
            continue
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            flush()
        if len(changes) >= limit:
            break
    flush()
    return changes[:limit]


async def push_branch(branch: str) -> None:
    """Отправить ветку заявки в origin.

    Единственная команда с токеном, которая касается работы агента, — и
    выполняется она в копии шлюза по имени ветки, а не в рабочей копии. Иначе
    достаточно было бы подменить в ней указатель `.git`, чтобы заголовок
    `Authorization` уехал на посторонний хост.
    """
    await git(
        "push", "--force-with-lease", "origin", f"refs/heads/{branch}:refs/heads/{branch}",
        cwd=settings.repo_dir, auth=True,
    )


async def delete_remote_branch(branch: str) -> None:
    await git("push", "origin", "--delete", branch, cwd=settings.repo_dir, auth=True, check=False)


async def repo_info() -> dict[str, Any]:
    """Диагностика для админки."""
    if not (settings.repo_dir / ".git").exists():
        return {"ok": False, "error": "локальная копия проекта ещё не склонирована"}
    try:
        head = (await git("rev-parse", "--short", f"origin/{settings.base_branch}", cwd=settings.repo_dir)).strip()
        last = (await git("log", "-1", "--pretty=%s (%cr)", f"origin/{settings.base_branch}", cwd=settings.repo_dir)).strip()
        return {"ok": True, "base": settings.base_branch, "head": head, "last_commit": last}
    except GitError as exc:
        return {"ok": False, "error": str(exc)}
