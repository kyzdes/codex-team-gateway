"""Сквозной прогон пайплайна без GitHub и без настоящего Codex.

Поднимает локальный bare-репозиторий вместо GitHub, подменяет Codex
скриптом-заглушкой, который печатает такие же JSONL-события, и проверяет,
что заявка проходит весь путь: работа → вопрос → правка → PR → выкатка.

Запуск:  .venv/bin/python tests/e2e.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="gateway-e2e-"))

FAKE_CODEX = WORK / "fake-codex"
ORIGIN = WORK / "origin.git"
SEED = WORK / "seed"

os.environ.update(
    {
        "DATA_DIR": str(WORK / "data"),
        "PROJECT_REPO": "acme/demo",
        "PROJECT_GIT_URL": str(ORIGIN),
        "PROJECT_BASE_BRANCH": "main",
        "PROJECT_TEST_CMD": "",
        "GITHUB_TOKEN": "dummy-token",
        "GITHUB_REQUIRED_CHECK": "tests",
        "USERS": "anna:Анна",
        "ADMIN_TOKEN": "admin-token",
        "CODEX_BIN": str(FAKE_CODEX),
        "AGENT_USER": "",
        "CODEX_TIMEOUT": "120",
        "PROJECT_PROD_URL": "",
    }
)
sys.path.insert(0, str(ROOT))

FAKE_CODEX_SOURCE = '''#!/usr/bin/env python3
"""Заглушка Codex: печатает события того же формата, что `codex exec --json`."""
import json, subprocess, sys, pathlib

resume = "resume" in sys.argv
images = sys.argv.count("-i")
# Текст заявки — последний позиционный аргумент перед флагами картинок.
prompt = sys.argv[-1 - images * 2]
thread = "th-1"

def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

emit({"type": "thread.started", "thread_id": thread})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "i0", "type": "command_execution",
      "command": "/bin/bash -lc 'rg -n phone index.html'", "exit_code": 0, "status": "completed"}})

if "ВОПРОС" in prompt and not resume:
    emit({"type": "item.completed", "item": {"id": "i1", "type": "agent_message",
          "text": json.dumps({"status": "question", "question": "Какой телефон поставить?",
                              "title": "Смена телефона", "summary": "", "user_visible": [],
                              "risk": "low", "notes": ""}, ensure_ascii=False)}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    raise SystemExit(0)

page = pathlib.Path("index.html")
page.write_text(page.read_text(encoding="utf-8").replace("+7 000 000-00-00", "+7 999 123-45-67"), encoding="utf-8")
emit({"type": "item.completed", "item": {"id": "i2", "type": "file_change",
      "changes": [{"path": str(page.resolve()), "kind": "modify"}], "status": "completed"}})
subprocess.run(["git", "add", "-A"], check=True)
subprocess.run(["git", "commit", "-q", "-m", "Обновил телефон на странице"], check=True)
emit({"type": "item.completed", "item": {"id": "i3", "type": "command_execution",
      "command": "/bin/bash -lc 'git commit -m ...'", "exit_code": 0, "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "i4", "type": "agent_message",
      "text": json.dumps({"status": "done", "title": "Новый телефон на главной",
                          "summary": "Заменил телефон на главной странице.",
                          "user_visible": ["В шапке сайта новый номер"],
                          "risk": "low", "notes": f"images={images}"}, ensure_ascii=False)}})
emit({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}})
'''


def sh(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=str(cwd) if cwd else None, check=True, capture_output=True)


def prepare() -> None:
    FAKE_CODEX.write_text(FAKE_CODEX_SOURCE, encoding="utf-8")
    FAKE_CODEX.chmod(0o755)

    SEED.mkdir(parents=True)
    (SEED / "index.html").write_text(
        "<html><body><h1>Демо</h1><p>Телефон: +7 000 000-00-00</p></body></html>\n",
        encoding="utf-8",
    )
    sh("git", "init", "-q", "-b", "main", cwd=SEED)
    sh("git", "config", "user.email", "seed@example.com", cwd=SEED)
    sh("git", "config", "user.name", "Seed", cwd=SEED)
    sh("git", "add", "-A", cwd=SEED)
    sh("git", "commit", "-q", "-m", "Первая версия", cwd=SEED)
    sh("git", "init", "-q", "--bare", str(ORIGIN))
    sh("git", "remote", "add", "origin", str(ORIGIN), cwd=SEED)
    sh("git", "push", "-q", "origin", "main", cwd=SEED)


def check(condition: bool, label: str) -> None:
    print(f"  {'✓' if condition else '✗'} {label}")
    if not condition:
        raise SystemExit(f"ПРОВАЛ: {label}")


async def main() -> None:
    prepare()
    from app import db, github, pipeline  # импорт только после настройки окружения

    merged: dict[str, object] = {}

    async def fake_create_pr(branch: str, title: str, body: str) -> dict[str, object]:
        merged["pr_body"] = body
        return {"number": 42, "url": "https://github.com/acme/demo/pull/42", "head_sha": "deadbeef"}

    async def fake_merge(number: int, title: str) -> str:
        merged["merged"] = number
        return "mergesha"

    github.create_pull_request = fake_create_pr  # type: ignore[assignment]
    github.merge = fake_merge  # type: ignore[assignment]

    print("\n1. Заявка с уточняющим вопросом")
    rid = db.create_request("anna", "ВОПРОС: поменяй телефон на сайте")
    await pipeline._process(rid, None)
    request = db.get_request(rid)
    check(request["status"] == pipeline.NEEDS_INPUT, "статус — «нужен ответ»")
    check("телефон" in (request["question"] or "").lower(), "вопрос сохранён")

    print("\n2. Ответ пользователя и доведение до PR")
    await pipeline._process(rid, "Поставь +7 999 123-45-67")
    request = db.get_request(rid)
    check(request["status"] == pipeline.CHECKING, f"статус — «идёт проверка» (получено: {request['status']})")
    check(request["pr_number"] == 42, "PR открыт")
    check(any("index.html" in f for f in request["files"]), "изменённый файл виден шлюзу")
    texts = request["text_changes"]
    check(bool(texts) and "999" in texts[0]["after"], "новый текст попал в «было → стало»")
    check(request["title"] == "Новый телефон на главной", "заголовок от агента")

    print("\n3. Ветка реально ушла в origin")
    branches = subprocess.run(
        ["git", "branch", "--list", "codex/*"], cwd=str(ORIGIN), capture_output=True, text=True
    ).stdout
    check(f"codex/req-{rid}" in branches, "ветка есть в удалённом репозитории")

    print("\n4. Зелёная проверка → ожидание подтверждения")
    async def fake_check_state(sha: str) -> dict[str, str]:
        return {"status": "success", "detail": "", "url": ""}

    github.check_state = fake_check_state  # type: ignore[assignment]
    await pipeline._refresh_checks(db.get_request(rid))
    check(db.get_request(rid)["status"] == pipeline.REVIEW, "статус — «ждёт подтверждения»")

    print("\n5. Подтверждение и выкатка")
    await pipeline._approve(rid)
    request = db.get_request(rid)
    check(merged.get("merged") == 42, "мерж вызван")
    check(request["status"] == pipeline.DONE, f"статус — «готово» (получено: {request['status']})")
    check(bool(request["merged_at"]) and bool(request["deployed_at"]), "проставлены отметки времени")
    check(not pipeline.gitops.worktree_path(rid).exists(), "рабочая папка убрана за собой")

    print("\n6. Лента событий для человека")
    events = db.list_events(rid)
    texts = [event["text"] for event in events]
    check(any("Изучаю проект" in t for t in texts), "команды переведены на человеческий язык")
    check(not any("/bin/bash" in t for t in texts), "сырых shell-команд в ленте нет")

    print("\n7. Заявка со скриншотом")
    from app import uploads

    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    staged = uploads.save_staged("anna", png)
    check(staged.endswith(".png"), "картинка принята и переименована")
    check(uploads.staged_path("anna", "../../etc/passwd") is None, "выход из каталога не проходит")
    try:
        uploads.save_staged("anna", "<html>не картинка</html>".encode())
        check(False, "не-картинка отклонена")
    except uploads.UploadError:
        check(True, "не-картинка отклонена")

    rid3 = db.create_request("anna", "Вот скриншот, поправьте телефон как на нём")
    attached = uploads.attach(rid3, "anna", [staged])
    db.update_request(rid3, images=attached)
    await pipeline._process(rid3, None)
    request3 = db.get_request(rid3)
    check(request3["notes"] == "images=1", f"агент получил картинку флагом -i (получено: {request3['notes']})")
    check(request3["images"] == attached, "картинка привязана к заявке")

    print("\n8. Заявка, где менять нечего")
    rid2 = db.create_request("anna", "Поменяй телефон на сайте")
    await pipeline._process(rid2, None)
    await pipeline._process(rid2, None)  # второй прогон: правка уже на месте
    check(db.get_request(rid2)["status"] in {pipeline.CHECKING, pipeline.NO_CHANGES}, "повторный прогон не падает")

    print("\nВсё прошло.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
