"""Запуск Codex CLI и перевод его событий на человеческий язык.

Codex вызывается в режиме `exec --json`: он печатает поток JSONL-событий,
мы их разбираем на лету и отдаём в интерфейс короткими понятными строчками
(«Правлю файл», «Прогоняю тесты»), а не сырыми shell-командами.

Важно: у процесса Codex НЕТ доступа к GitHub-токену. Он только меняет файлы
в своей папке и коммитит локально; пуш, PR и мерж делает сам шлюз.

Тем же способом обязано запускаться всё остальное, что приходит из проекта
клиента (например, его тестовая команда): `agent_command` + `clean_env`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import settings

logger = logging.getLogger(__name__)

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


# Живые прогоны агента: без них force_cancel не смог бы снять зависшую заявку.
RUNNING: dict[int, asyncio.subprocess.Process] = {}

# Секреты шлюза, которым нечего делать в окружении агента: GitHub-токен даёт
# право пушить и мержить, ADMIN_TOKEN и USERS — входить в интерфейс за людей.
LEAKY_ENV = ("GITHUB_TOKEN", "DOKPLOY_TOKEN", "ADMIN_TOKEN", "USERS", "TELEGRAM_BOT_TOKEN")

# Сколько ждём процесс после SIGKILL. Ждать без предела нельзя: если снять его
# так и не вышло, ожидание держит слот очереди до перезапуска сервиса.
KILL_GRACE = 10

# Причины, о которых уже предупредили: isolation() спрашивают на каждую команду,
# а сказать про сломанную изоляцию нужно один раз и внятно.
_warned: set[str] = set()


def isolation() -> tuple[bool, str]:
    """Запускается ли агент отдельным пользователем — и если нет, то почему.

    Это главный предохранитель шлюза, и раньше он выключался молча: любая из
    четырёх причин просто давала пустой префикс. При совпадающем uid clean_env()
    не защищает ничего — дочерний процесс читает /proc/<pid шлюза>/environ и
    получает обратно GitHub-токен, ADMIN_TOKEN и токены сотрудников, потому что
    environ закрыт по пользователю, а не по процессу.
    """
    user = settings.agent_user
    if not user:
        return False, "AGENT_USER пуст — агент работает тем же пользователем, что и шлюз"
    if not shutil.which("sudo"):
        return False, "в системе нет sudo — запускать агента отдельным пользователем нечем"
    if not os.path.exists(settings.agent_wrapper):
        return False, f"нет обёртки {settings.agent_wrapper}"
    try:
        if pwd.getpwuid(os.geteuid()).pw_name == user:
            return False, f"шлюз сам работает пользователем {user} — разделения нет"
        pwd.getpwnam(user)
    except KeyError:
        return False, f"пользователя {user} нет в системе"
    return True, f"агент запускается пользователем {user}"


def _sudo_prefix() -> list[str]:
    """Codex запускается отдельным пользователем: так он физически не может
    прочитать окружение и файлы шлюза (GitHub-токен, ссылки доступа, базу).
    Если такого пользователя нет (локальная разработка) — работаем как есть,
    но говорим об этом в лог: молчаливое вырождение выглядит как рабочий режим."""
    ready, reason = isolation()
    if ready:
        return ["sudo", "-n", "-H", "-u", settings.agent_user, settings.agent_wrapper]
    if reason not in _warned:
        _warned.add(reason)
        logger.warning("Агент работает БЕЗ изоляции: %s", reason)
    return []


def agent_command(argv: list[str]) -> list[str]:
    """Обернуть argv в запуск от имени пользователя агента.

    Так обязано запускаться всё, что пришло из проекта клиента, а не только
    Codex: тестовая команда лежит в рабочей копии, которую агент только что
    правил, — по сути это чужой код, и от пользователя шлюза ему нельзя.
    """
    return [*_sudo_prefix(), *argv]


def clean_env() -> dict[str, str]:
    """Окружение для процессов агента: как у шлюза, но без его секретов."""
    env = dict(os.environ)
    for leaky in LEAKY_ENV:
        env.pop(leaky, None)
    return env


async def _kill_through_wrapper(pgid: int) -> bool:
    """Сигнал чужому процессу — от имени того же пользователя, что его запустил."""
    argv = agent_command(["kill", "-KILL", f"-{pgid}"])
    if argv[0] == "kill":  # обёртки нет, а напрямую уже не получилось
        return False
    killer = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await killer.wait() == 0


async def _signal_kill(proc: asyncio.subprocess.Process, pgid: int) -> bool:
    """Послать SIGKILL группе прогона. Возвращает, дошёл ли сигнал."""
    if pgid == os.getpgrp():
        # Такого быть не должно: каждый прогон стартует с start_new_session=True.
        # Но если группа всё же общая, групповой сигнал снял бы и сам шлюз.
        logger.error("Прогон %s остался в группе шлюза — снимаем только его самого", proc.pid)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
            return True
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except OSError:  # чужой пользователь — прямой сигнал не дошёл
        return await _kill_through_wrapper(pgid)


async def kill_group(proc: asyncio.subprocess.Process) -> bool:
    """Снять процесс вместе со всем, что он породил.

    Бьём по группе, а не по pid, по двум причинам сразу. Агент плодит
    подпроцессы, и одиночный kill оставляет их сиротами — они продолжают писать
    в рабочую копию, которую шлюз в этот же момент сносит. А под sudo процесс
    ещё и чужой: proc.kill() получает EPERM, и раньше эта ошибка глушилась, а
    следом шло безусловное ожидание живого процесса — прогон висел вечно.
    """
    if proc.returncode is not None:
        return False
    try:
        # Прогон стартует с start_new_session=True, поэтому лидер группы — он сам.
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    if not await _signal_kill(proc, pgid):
        return False
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE)
    return True


async def terminate(request_id: int) -> bool:
    """Снять зависший прогон. Возвращает, удалось ли добраться до процесса."""
    proc = RUNNING.get(request_id)
    return await kill_group(proc) if proc is not None else False


def build_command(prompt: str, thread_id: str | None, images: list[Path] | None = None) -> list[str]:
    cmd = agent_command([settings.codex_bin, "-a", "never", "-s", settings.codex_sandbox])
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
    # Важен порядок: `-i` принимает несколько значений подряд и, стоя перед
    # текстом, съедает его как ещё один файл. Поэтому только после промпта
    # и по одному флагу на картинку.
    for image in images or []:
        cmd += ["-i", str(image)]
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
    request_id: int,
    prompt: str,
    workdir: Path,
    thread_id: str | None,
    log_path: Path,
    on_progress: Progress,
    images: list[Path] | None = None,
) -> CodexResult:
    cmd = build_command(prompt, thread_id, images)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workdir),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=clean_env(),
        # Своя группа процессов: агент плодит подпроцессы, и снять его
        # принудительно можно только сигналом всей группе разом.
        start_new_session=True,
    )
    RUNNING[request_id] = proc

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
        await kill_group(proc)
    finally:
        log.close()
        RUNNING.pop(request_id, None)

    result.exit_code = proc.returncode if proc.returncode is not None else -1
    result.log_path = str(log_path)
    return result
