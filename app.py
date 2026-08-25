#!/usr/bin/env python3
"""
Codex Team Gateway
-------------------
Небольшой веб-шлюз, который даёт нескольким коллегам чат-интерфейс
для общения с Codex CLI, установленным локально на сервере (Mac mini / Ubuntu).

Как это работает:
  - У каждого пользователя свой bearer-токен и своя рабочая директория
    (обычно git worktree одного и того же репозитория) — это позволяет
    двум людям работать параллельно без конфликтов файлов.
  - Каждое сообщение выполняется через `codex exec` (неинтерактивный режим).
  - Первое сообщение пользователя создаёт новую сессию (thread), все
    следующие сообщения продолжают её через `codex exec resume <thread_id>`,
    поэтому Codex помнит контекст переписки с конкретным коллегой.
  - Все события (JSONL) и итоговый ответ логируются на диск — это единственная
    "страховка" при полностью автономном режиме работы Codex.
  - Запросы в одну и ту же рабочую директорию выполняются строго по одному
    (asyncio.Lock), чтобы не было двух параллельных `codex exec` в одной папке.

Запуск:
    pip install -r requirements.txt
    export CODEX_GATEWAY_CONFIG=/etc/codex-gateway/config.json
    uvicorn app:app --host 0.0.0.0 --port 8787
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONFIG_PATH = Path(os.environ.get("CODEX_GATEWAY_CONFIG", "./gateway_config.json"))
STATE_DIR = Path(os.environ.get("CODEX_GATEWAY_STATE_DIR", "./state"))
LOG_DIR = Path(os.environ.get("CODEX_GATEWAY_LOG_DIR", "./logs"))
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Codex Team Gateway")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Конфиг не найден: {CONFIG_PATH}. Скопируйте gateway_config.example.json "
            "и заполните токены/пути."
        )
    return json.loads(CONFIG_PATH.read_text())


CONFIG = load_config()
USERS = CONFIG["users"]  # {"alice": {"token": "...", "workdir": "...", "display_name": "..."}}
SANDBOX_MODE = CONFIG.get("sandbox_mode", "danger-full-access")  # см. README про риски
APPROVAL_POLICY = CONFIG.get("approval_policy", "never")
MODEL = CONFIG.get("model")  # None -> использовать модель по умолчанию

# Токен -> имя пользователя (быстрый поиск)
TOKEN_TO_USER = {u["token"]: name for name, u in USERS.items()}

# Один lock на рабочую директорию, чтобы не запускать два codex exec
# в одной и той же папке параллельно.
_locks: dict[str, asyncio.Lock] = {}


def lock_for(workdir: str) -> asyncio.Lock:
    if workdir not in _locks:
        _locks[workdir] = asyncio.Lock()
    return _locks[workdir]


def state_file(user: str) -> Path:
    return STATE_DIR / f"{user}.json"


def load_state(user: str) -> dict:
    f = state_file(user)
    if f.exists():
        return json.loads(f.read_text())
    return {"thread_id": None, "history": []}


def save_state(user: str, state: dict) -> None:
    state_file(user).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def user_log_dir(user: str) -> Path:
    d = LOG_DIR / user
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Запуск codex exec
# ---------------------------------------------------------------------------

def build_command(thread_id: Optional[str], message: str) -> list[str]:
    cmd = [CODEX_BIN, "-a", APPROVAL_POLICY, "-s", SANDBOX_MODE]
    if MODEL:
        cmd += ["-m", MODEL]
    cmd += ["exec", "--json", "--skip-git-repo-check"]
    if thread_id:
        cmd += ["resume", thread_id, message]
    else:
        cmd += [message]
    return cmd


async def run_codex(user: str, workdir: str, message: str) -> dict:
    state = load_state(user)
    thread_id = state.get("thread_id")
    cmd = build_command(thread_id, message)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    # Сохраняем полный сырой лог на диск — это то, что позволяет потом
    # разобраться, что именно сделал Codex по запросу коллеги.
    ts = time.strftime("%Y%m%dT%H%M%S")
    log_path = user_log_dir(user) / f"{ts}-{uuid.uuid4().hex[:8]}.jsonl"
    log_path.write_text(stdout)
    if stderr.strip():
        (log_path.with_suffix(".stderr.log")).write_text(stderr)

    reply_text = None
    commands_run = []
    new_thread_id = thread_id

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "thread.started":
            new_thread_id = event.get("thread_id", new_thread_id)
        elif etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                reply_text = item.get("text")
            elif item.get("type") == "command_execution":
                commands_run.append(
                    {
                        "command": item.get("command"),
                        "status": item.get("status"),
                    }
                )

    if new_thread_id and new_thread_id != thread_id:
        state["thread_id"] = new_thread_id
    state.setdefault("history", []).append(
        {"ts": ts, "message": message, "reply": reply_text, "log": str(log_path)}
    )
    save_state(user, state)

    if reply_text is None:
        reply_text = (
            "Codex не вернул текстовый ответ. Проверьте лог: " + str(log_path)
            + (f"\n\nstderr:\n{stderr}" if stderr.strip() else "")
        )

    return {
        "reply": reply_text,
        "commands": commands_run,
        "thread_id": new_thread_id,
        "exit_code": proc.returncode,
        "log_file": str(log_path),
    }


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


def authenticate(authorization: Optional[str]) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Нужен заголовок Authorization: Bearer <token>")
    token = authorization.removeprefix("Bearer ").strip()
    user = TOKEN_TO_USER.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Неверный токен")
    return user


@app.post("/api/chat")
async def chat(req: ChatRequest, authorization: Optional[str] = Header(None)):
    user = authenticate(authorization)
    workdir = USERS[user]["workdir"]
    if not os.path.isdir(workdir):
        raise HTTPException(status_code=500, detail=f"Рабочая директория не найдена: {workdir}")

    async with lock_for(workdir):
        result = await run_codex(user, workdir, req.message)
    return result


@app.get("/api/history")
async def history(authorization: Optional[str] = Header(None)):
    user = authenticate(authorization)
    state = load_state(user)
    return {"display_name": USERS[user].get("display_name", user), "history": state.get("history", [])}


@app.get("/api/health-check")
async def health_check():
    return {"status": "ok"}


@app.get("/api/whoami")
async def whoami(authorization: Optional[str] = Header(None)):
    user = authenticate(authorization)
    return {"user": user, "display_name": USERS[user].get("display_name", user)}


# Статика (простой чат-интерфейс)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(static_dir / "index.html"))
