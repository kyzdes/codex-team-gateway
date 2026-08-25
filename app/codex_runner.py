"""Запуск Codex CLI и перевод его событий на человеческий язык.

Codex вызывается в режиме `exec --json`: он печатает поток JSONL-событий,
мы их разбираем на лету и отдаём в интерфейс короткими понятными строчками
(«Правлю файл», «Прогоняю тесты»), а не сырыми shell-командами.

Важно: у процесса Codex НЕТ доступа к GitHub-токену. Он только меняет файлы
в своей папке и коммитит локально; пуш, PR и мерж делает сам шлюз.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pwd
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings

Progress = Callable[[str, str], Awaitable[None]]  # (kind, text)

_SHELL_WRAPPER = re.compile(r"^/[\w/]*(?:bash|sh|zsh)\s+-l?c\s+")
_READ_ONLY = re.compile(
    r"^(cat|ls|rg|grep|find|head|tail|sed -n|wc|git (status|diff|log|show|rev-parse|branch))\b"
)
_TEST_HINT = re.compile(r"(pytest|run_tests|npm (run )?test|yarn test|go test|make test|vitest|jest)")
_GIT_WRITE = re.compile(r"^git (add|commit|checkout|switch|restore|apply)\b")


@dataclass
class CodexResult:
    thread_id: str | None = None
    final_text: str = ""
    exit_code: int = 0
    log_path: str = ""
    timed_out: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    files_touched: list[str] = field(default_factory=list)
    stderr_tail: str = ""


AGENT_WRAPPER = "/usr/local/bin/run-agent.sh"


def _sudo_prefix() -> list[str]:
    """Codex запускается отдельным пользователем: так он физически не может
    прочитать окружение и файлы шлюза (GitHub-токен, ссылки доступа, базу).
    Если такого пользователя нет (локальная разработка) — работаем как есть."""
    user = settings.agent_user
    if not user or not shutil.which("sudo") or not os.path.exists(AGENT_WRAPPER):
        return []
    try:
        if pwd.getpwuid(os.geteuid()).pw_name == user:
            return []
        pwd.getpwnam(user)
    except KeyError:
        return []
    return ["sudo", "-n", "-H", "-u", user, AGENT_WRAPPER]


def build_command(prompt: str, thread_id: str | None) -> list[str]:
    cmd = [*_sudo_prefix(), settings.codex_bin, "-a", "never", "-s", settings.codex_sandbox]
    if settings.codex_sandbox == "workspace-write":
        flag = "true" if settings.codex_network else "false"
        cmd += ["-c", f"sandbox_workspace_write.network_access={flag}"]
    if settings.codex_model:
        cmd += ["-m", settings.codex_model]
    cmd += ["exec", "--json", "--skip-git-repo-check"]
    if thread_id:
        cmd += ["resume", thread_id, prompt]
    else:
        cmd += [prompt]
    return cmd


def _clean_command(raw: str) -> str:
    cleaned = _SHELL_WRAPPER.sub("", raw or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] == "'":
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def describe_command(raw: str, exit_code: int | None) -> str | None:
    """Короткая человеческая формулировка того, что сейчас делает агент."""
    cmd = _clean_command(raw)
    if not cmd:
        return None
    first_line = cmd.splitlines()[0]
    if exit_code not in (None, 0) and not _READ_ONLY.match(first_line):
        return "Проверка не прошла, разбираюсь"
    if _TEST_HINT.search(first_line):
        return "Прогоняю тесты"
    if _GIT_WRITE.match(first_line):
        return "Сохраняю изменения"
    if _READ_ONLY.match(first_line):
        return "Изучаю проект"
    short = first_line if len(first_line) <= 70 else first_line[:67] + "…"
    return f"Выполняю: {short}"


def describe_files(changes: list[dict[str, Any]]) -> str | None:
    names = [Path(c.get("path", "")).name for c in changes if c.get("path")]
    names = [n for n in names if n]
    if not names:
        return None
    shown = ", ".join(names[:3])
    if len(names) > 3:
        shown += f" и ещё {len(names) - 3}"
    return f"Правлю: {shown}"


def looks_like_result_json(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").lstrip("json").strip()
    return stripped.startswith("{") and stripped.endswith("}")


def parse_result_json(text: str) -> dict[str, Any] | None:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def run(
    prompt: str,
    workdir: Path,
    thread_id: str | None,
    log_path: Path,
    on_progress: Progress,
) -> CodexResult:
    cmd = build_command(prompt, thread_id)
    env = dict(os.environ)
    # Ни один секрет шлюза не должен попасть в окружение агента.
    for leaky in ("GITHUB_TOKEN", "DOKPLOY_TOKEN", "ADMIN_TOKEN", "USERS", "TELEGRAM_BOT_TOKEN"):
        env.pop(leaky, None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workdir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    result = CodexResult(thread_id=thread_id)
    last_progress = ""
    log = log_path.open("w", encoding="utf-8")

    async def pump_stdout() -> None:
        nonlocal last_progress
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace")
            log.write(line)
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            if etype == "thread.started":
                result.thread_id = event.get("thread_id") or result.thread_id
                continue
            if etype == "turn.completed":
                result.usage = event.get("usage") or {}
                continue
            if etype != "item.completed":
                continue

            item = event.get("item") or {}
            itype = item.get("type")
            text: str | None = None

            if itype == "agent_message":
                message = item.get("text") or ""
                result.final_text = message
                if not looks_like_result_json(message):
                    await on_progress("agent", message.strip())
                continue
            if itype == "file_change":
                changes = item.get("changes") or []
                for change in changes:
                    path = change.get("path")
                    if path:
                        result.files_touched.append(path)
                text = describe_files(changes)
            elif itype == "command_execution":
                text = describe_command(item.get("command", ""), item.get("exit_code"))
            elif itype == "error":
                await on_progress("error", str(item.get("message", "Ошибка агента")))
                continue

            if text and text != last_progress:
                last_progress = text
                await on_progress("progress", text)

    async def pump_stderr() -> None:
        assert proc.stderr is not None
        tail: list[str] = []
        async for raw_line in proc.stderr:
            tail.append(raw_line.decode("utf-8", errors="replace"))
            del tail[:-40]
        result.stderr_tail = "".join(tail).strip()

    try:
        await asyncio.wait_for(
            asyncio.gather(pump_stdout(), pump_stderr(), proc.wait()),
            timeout=settings.codex_timeout,
        )
    except asyncio.TimeoutError:
        result.timed_out = True
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        await proc.wait()
    finally:
        log.close()

    result.exit_code = proc.returncode if proc.returncode is not None else -1
    result.log_path = str(log_path)
    return result
