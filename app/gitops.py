"""Работа с git: зеркало проекта, worktree на заявку, факты об изменениях, пуш.

Каждая заявка = своя ветка от актуального origin/<base> и своя папка-worktree.
Так две заявки не перемешиваются между собой, а мерж одной не тащит за собой
недоделанную вторую.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

COMMIT_NAME = "Codex (заявка)"
COMMIT_EMAIL = "codex@localhost"

# Файлы, из которых имеет смысл показывать текстовую разницу человеку.
TEXTY = re.compile(r"\.(html?|jinja2?|j2|twig|vue|svelte|jsx|tsx|md|txt|ya?ml|json|po|csv|py|js|ts|css)$", re.I)
CODE_NOISE = re.compile(r"^\s*(import|from|def |class |return|const |let |var |@|#|//|/\*|\*|\}|\{|\)|\()")


class GitError(RuntimeError):
    pass


@dataclass
class Facts:
    """То, что шлюз знает про изменения сам, не веря агенту на слово."""

    commits: int = 0
    head_sha: str = ""
    files: list[dict[str, str]] = field(default_factory=list)
    text_changes: list[dict[str, str]] = field(default_factory=list)


def _auth_env() -> dict[str, str] | None:
    """Учётка для одной команды — через окружение, а не через аргументы.

    Раньше токен уезжал в argv (`git -c http.extraHeader=...`), а /proc/<pid>/cmdline
    на Linux читает любой пользователь — то есть пользователь агента доставал
    оттуда GitHub-токен шлюза и мог пушить в репозиторий клиента мимо PR и мимо
    кнопки человека. Окружение процесса закрыто по uid, поэтому GIT_CONFIG_*
    (git ≥ 2.31) не видно никому, кроме самого шлюза.
    """
    if not settings.github_token:
        return None
    basic = base64.b64encode(f"x-access-token:{settings.github_token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


async def git(*args: str, cwd: Path | None = None, auth: bool = False, check: bool = True) -> str:
    env: dict[str, str] | None = None
    if auth:
        extra = _auth_env()
        if extra:
            env = {**os.environ, **extra}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
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
        if settings.github_token:
            message = message.replace(settings.github_token, "***")
        raise GitError(f"git {' '.join(args[:3])}: {message}")
    return stdout


async def ensure_repo() -> None:
    """Клонирует проект при первом запуске, дальше просто обновляет."""
    if not (settings.repo_dir / ".git").exists():
        settings.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        await git("clone", settings.clone_url, str(settings.repo_dir), auth=True)
    await git("config", "user.name", COMMIT_NAME, cwd=settings.repo_dir)
    await git("config", "user.email", COMMIT_EMAIL, cwd=settings.repo_dir)
    await fetch()


async def fetch() -> None:
    await git("fetch", "origin", "--prune", cwd=settings.repo_dir, auth=True)


def branch_name(request_id: int) -> str:
    return f"codex/req-{request_id}"


def worktree_path(request_id: int) -> Path:
    return settings.worktrees_dir / f"req-{request_id}"


async def create_worktree(request_id: int) -> tuple[Path, str]:
    await fetch()
    path = worktree_path(request_id)
    branch = branch_name(request_id)
    if path.exists():
        await remove_worktree(request_id)
    # Ветка могла остаться от прошлой попытки — снимаем её, чтобы начать с чистого.
    await git("branch", "-D", branch, cwd=settings.repo_dir, check=False)
    await git(
        "worktree", "add", "-b", branch, str(path), f"origin/{settings.base_branch}",
        cwd=settings.repo_dir,
    )
    await git("config", "user.name", COMMIT_NAME, cwd=path)
    await git("config", "user.email", COMMIT_EMAIL, cwd=path)
    return path, branch


async def remove_worktree(request_id: int) -> None:
    """Снять рабочую копию заявки. Зовётся на каждом финале заявки, поэтому
    обязана быть безобидной: worktree могло не быть вовсе, а репозитория —
    ещё не появиться (первый запуск, клон не прошёл)."""
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


async def has_uncommitted(path: Path) -> bool:
    return bool((await git("status", "--porcelain", cwd=path)).strip())


async def commit_all(path: Path, message: str) -> None:
    await git("add", "-A", cwd=path)
    await git("commit", "-m", message, cwd=path)


async def collect_facts(path: Path) -> Facts:
    """Что реально изменилось — по данным git, а не по словам агента."""
    base = f"origin/{settings.base_branch}"
    facts = Facts()

    count = (await git("rev-list", "--count", f"{base}..HEAD", cwd=path)).strip()
    facts.commits = int(count or 0)
    if facts.commits == 0:
        return facts

    facts.head_sha = (await git("rev-parse", "HEAD", cwd=path)).strip()

    for line in (await git("diff", "--name-status", f"{base}...HEAD", cwd=path)).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            facts.files.append({"status": parts[0][:1], "path": parts[-1]})

    facts.text_changes = _extract_text_changes(
        await git("diff", "-U0", f"{base}...HEAD", cwd=path)
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


async def push_branch(path: Path, branch: str) -> None:
    await git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}", cwd=path, auth=True)


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
