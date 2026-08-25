"""Сквозной прогон пайплайна без GitHub и без настоящего Codex.

Поднимает локальный bare-репозиторий вместо GitHub, подменяет Codex
скриптом-заглушкой, который печатает такие же JSONL-события, и проверяет,
что заявка проходит весь путь: работа → вопрос → правка → PR → выкатка.

Запуск:  .venv/bin/python tests/e2e.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

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
        "RATE_LIMIT_PER_HOUR": "3",
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

if "СЛОМАЙ" in prompt:
    print("codex: не смог разобраться с проектом", file=sys.stderr)
    raise SystemExit(3)

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


async def drain(pipeline: Any) -> None:
    """Дождаться фоновых задач пайплайна.

    start() и approve() по контракту ничего не ждут — они ставят работу в цикл
    событий и сразу возвращают управление интерфейсу. Тесту, наоборот, нужен
    результат, поэтому досматриваем задачи до конца.
    """
    while pipeline._tasks:
        await asyncio.gather(*list(pipeline._tasks))
        await asyncio.sleep(0)  # даём отработать колбэкам, снимающим задачи с учёта


def check(condition: bool, label: str) -> None:
    print(f"  {'✓' if condition else '✗'} {label}")
    if not condition:
        raise SystemExit(f"ПРОВАЛ: {label}")


async def main() -> None:
    prepare()
    # Импорт только после настройки окружения: config читает его на уровне модуля.
    from app import codex_runner, db, github, pipeline
    from app.config import settings

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
    await pipeline.refresh_checks(db.get_request(rid))
    check(db.get_request(rid)["status"] == pipeline.REVIEW, "статус — «ждёт подтверждения»")

    print("\n5. Подтверждение и выкатка")
    pipeline.approve(rid, approved_by="admin")
    await drain(pipeline)
    request = db.get_request(rid)
    check(merged.get("merged") == 42, "мерж вызван")
    check(request["status"] == pipeline.DONE, f"статус — «готово» (получено: {request['status']})")
    check(bool(request["merged_at"]) and bool(request["deployed_at"]), "проставлены отметки времени")
    check(not pipeline.gitops.worktree_path(rid).exists(), "рабочая папка убрана за собой")
    check(request["approved_by"] == "admin", "записано, кто нажал «Выкатить»")
    check(bool(request["approved_at"]), "записано, когда подтвердили")
    journal = db.approvals_journal(10)
    check(any(entry["id"] == rid for entry in journal), "выкатка попала в журнал подтверждений")

    print("\n5а. Расход агента посчитан")
    spent = request["usage"]
    # Заявка прошла два прогона: вопрос и правку после ответа.
    check(spent.get("turns") == 2, f"считаны оба прогона (получено: {spent.get('turns')})")
    check(spent.get("input_tokens") == 30, f"токены сложены (получено: {spent.get('input_tokens')})")
    check(not codex_runner.RUNNING, "живых процессов агента не осталось")

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

    print("\n9. Пауза приёма: агента не запускаем")
    db.set_setting("intake_paused", "1")
    check(pipeline.paused(), "пауза видна пайплайну")
    rid4 = db.create_request("anna", "Поправьте телефон, пока приём закрыт")
    pipeline.start(rid4)
    await drain(pipeline)
    held = db.get_request(rid4)
    check(held["status"] == pipeline.QUEUED, f"заявка ждёт в очереди (получено: {held['status']})")
    check(not pipeline.gitops.worktree_path(rid4).exists(), "рабочая копия не создавалась")
    check(held["usage"] == {}, "агент не запускался — расход нулевой")
    check(
        any("приостановлен" in event["text"] for event in db.list_events(rid4)),
        "человеку сказали, почему заявка стоит",
    )

    db.set_setting("intake_paused", "0")
    check(pipeline.resume_queued() == 1, "снятие паузы подняло ровно одну ждавшую заявку")
    # Заявка ещё числится в очереди — прогон только поставлен в цикл событий.
    # Второй запуск дал бы ей второго агента в той же рабочей копии.
    check(pipeline.resume_queued() == 0, "повторное снятие паузы не запускает ту же заявку дважды")
    await drain(pipeline)
    resumed = db.get_request(rid4)
    check(resumed["status"] == pipeline.CHECKING, f"заявка доехала до PR (получено: {resumed['status']})")

    print("\n10. Лимит заявок в час")
    # Импорт HTTP-слоя здесь и проверяет, что он вообще собирается на этих модулях.
    from app import main as api

    limit = settings.rate_limit_per_hour
    worker = api.Principal(login="rate-test", display_name="Проверка лимита", role="user")
    for _ in range(limit):
        db.create_request(worker.login, "Заявка в пределах лимита")
    try:
        api.enforce_rate_limit(worker)
        check(False, "лимит отдаёт отказ")
    except api.HTTPException as exc:
        check(exc.status_code == 429, f"лимит отдаёт 429 (получено: {exc.status_code})")
        check("час" in exc.detail, "в отказе объяснено, что предел часовой")
    boss = api.Principal(login="admin", display_name="Администратор", role="admin")
    for _ in range(limit + 1):
        db.create_request(boss.login, "Админская заявка")
    api.enforce_rate_limit(boss)
    check(True, "администратора лимит не касается")

    print("\n11. Маршруты API на месте")
    paths = {getattr(route, "path", "") for route in api.app.routes}
    for path in (
        "/api/meta",
        "/api/admin/readiness",
        "/api/admin/usage",
        "/api/admin/journal",
        "/api/admin/people",
        "/api/admin/people/{login}/disable",
        "/api/admin/people/{login}/rotate",
        "/api/admin/pause",
        "/api/admin/requests/{request_id}/log",
        "/api/admin/requests/{request_id}/tests",
        "/api/admin/requests/{request_id}/force-cancel",
        "/api/admin/requests/{request_id}/recheck",
    ):
        check(path in paths, f"есть {path}")

    print("\n12. Тесты проекта идут без секретов шлюза")

    async def run_tests_with(command: str, allow_unisolated: bool = True) -> tuple[str, str]:
        """_run_tests с временно подменённой тестовой командой клиента.

        Команду задаёт клиент, а исполняется она поверх кода, который агент
        только что правил, — поэтому проверяем и окружение процесса, и то, что
        без отдельного пользователя агента шлюз её вовсе не запускает.
        """
        original = pipeline.settings
        pipeline.settings = dataclasses.replace(
            original, test_cmd=command, allow_unisolated_agent=allow_unisolated
        )
        try:
            return await pipeline._run_tests(WORK, rid2)
        finally:
            pipeline.settings = original

    probe = "echo GT=[$GITHUB_TOKEN] AT=[$ADMIN_TOKEN] US=[$USERS] HOME=[${HOME:+есть}]"
    verdict, output = await run_tests_with(probe)
    check(verdict == "ok", f"команда клиента выполнилась (получено: {verdict})")
    check("GT=[] AT=[] US=[]" in output, f"секретов шлюза в окружении нет: {output.strip()[-120:]}")
    check("HOME=[есть]" in output, "остальное окружение на месте")
    verdict, _ = await run_tests_with("exit 1")
    check(verdict == "failed", "падение тестовой команды шлюз замечает")
    # AGENT_USER здесь пуст, то есть агент работает тем же пользователем, что и
    # шлюз: код проекта в таком режиме запускать нельзя — он прочитает секреты
    # шлюза прямо из /proc, сколько окружение ни чисти.
    marker = WORK / "должно-остаться-неисполненным"
    verdict, _ = await run_tests_with(f"touch {marker}", allow_unisolated=False)
    check(verdict == "skipped", f"без изоляции агента команду клиента не запускаем (получено: {verdict})")
    check(not marker.exists(), "команда клиента действительно не выполнялась")

    print("\n13. Упавшая заявка убирает за собой")
    rid5 = db.create_request("anna", "СЛОМАЙ: пусть агент свалится")
    pipeline.start(rid5)
    await drain(pipeline)
    broken = db.get_request(rid5)
    check(broken["status"] == pipeline.FAILED, f"статус — «ошибка» (получено: {broken['status']})")
    check(not pipeline.gitops.worktree_path(rid5).exists(), "рабочая папка убрана и после падения")
    check(bool(broken["error"]), "человеку записано, что именно случилось")
    check(not codex_runner.RUNNING, "процесс упавшего прогона снят с учёта")

    print("\n14. Лента не умирает молча на отставшем клиенте")
    from app import bus

    queue = bus.subscribe()
    check(bus.is_subscribed(queue), "подписка живая")
    for number in range(bus.QUEUE_LIMIT + 5):
        bus.publish({"type": "event", "text": f"событие {number}"})
    # Раньше отписка была односторонней: генератор в main.py её не замечал и
    # вечно ждал на queue.get(), а лента у человека просто замирала.
    check(not bus.is_subscribed(queue), "переполнившего очередь клиента отписали")
    check(queue.empty(), "накопленное выброшено — генератор выйдет сразу, а не после протухшего хвоста")

    print("\n15. Завершённую заявку не воскресить проверками")
    settled = db.get_request(rid)
    await pipeline.refresh_checks(settled)
    check(db.get_request(rid)["status"] == pipeline.DONE, "«Проверить заново» не вернуло готовую заявку в review")
    try:
        await api.admin_recheck(rid)
        check(False, "админка отбивает проверку завершённой заявки")
    except api.HTTPException as exc:
        check(exc.status_code == 409, f"кнопка отдаёт 409 (получено: {exc.status_code})")

    print("\n16. Выкатка не идёт по второму кругу")
    # Сторож мог увидеть смерженный PR уже после выкатки кнопкой: снимок у него
    # старый, а хвост общий — и второй заход переписал бы отметки и статус.
    stale = {**settled, "status": pipeline.REVIEW}
    await pipeline._after_merge(rid, stale)
    again = db.get_request(rid)
    check(again["status"] == pipeline.DONE, f"статус остался «готово» (получено: {again['status']})")
    check(again["merged_at"] == settled["merged_at"], "отметка о мерже не переписана")

    print("\n17. Повтор заявки — один раз на заявку")
    first = await api.retry(rid5, boss)
    check(first["id"] != rid5, "повтор завёл новую заявку")
    check(bool(db.get_request(rid5)["retried_at"]), "исходная заявка помечена повторённой")
    try:
        await api.retry(rid5, boss)
        check(False, "второй повтор отбит")
    except api.HTTPException as exc:
        check(exc.status_code == 409, f"второй повтор отдаёт 409 (получено: {exc.status_code})")
    await drain(pipeline)

    print("\n18. Красная проверка убирает рабочую копию")

    async def fake_failed_state(sha: str) -> dict[str, str]:
        return {"status": "failure", "detail": "job «tests» упал", "url": ""}

    github.check_state = fake_failed_state  # type: ignore[assignment]
    check(pipeline.gitops.worktree_path(rid4).exists(), "рабочая копия заявки на месте")
    await pipeline.refresh_checks(db.get_request(rid4))
    red = db.get_request(rid4)
    check(red["status"] == pipeline.TESTS_FAILED, f"статус — «проверка не прошла» (получено: {red['status']})")
    check(not pipeline.gitops.worktree_path(rid4).exists(), "рабочая копия снята и после красной проверки")

    print("\n19. Картинки живой заявки переживают уборку по сроку")
    alive = db.create_request("anna", "Заявка ждёт ответа, скриншот ещё нужен")
    kept = uploads.attach(alive, "anna", [uploads.save_staged("anna", png)])
    db.update_request(alive, images=kept, status=pipeline.NEEDS_INPUT)
    folder = uploads.request_dir(alive)
    os.utime(folder, (0, 0))
    for item in folder.iterdir():
        os.utime(item, (0, 0))
    check(uploads.purge_old(1, pipeline._moving_requests()) == 0, "папку незавершённой заявки не тронули")
    check(db.get_request(alive)["images"] == kept, "список картинок в базе цел")
    db.update_request(alive, status=pipeline.DONE)
    check(uploads.purge_old(1, pipeline._moving_requests()) == 1, "папку завершённой заявки убрали")
    check(db.get_request(alive)["images"] == [], "битые ссылки на картинки из базы вычищены")

    print("\n20. Снятие прогона убивает и подпроцессы агента")

    def alive_in_group(pgid: int) -> int:
        """Сколько процессов ещё числится в группе прогона."""
        found = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True)
        return len(found.stdout.split())

    # Агент плодит подпроцессы, и раньше сигнал уходил одному pid: внуки
    # оставались жить, а рабочую копию у них тут же сносили из-под ног.
    runner = await asyncio.create_subprocess_exec(
        "bash", "-c", "sleep 60 & sleep 60",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    await asyncio.sleep(0.5)
    check(alive_in_group(runner.pid) > 1, "у прогона появились подпроцессы")
    codex_runner.RUNNING[0] = runner
    check(await codex_runner.terminate(0), "прогон снят")
    codex_runner.RUNNING.pop(0, None)
    for _ in range(20):
        if alive_in_group(runner.pid) == 0:
            break
        await asyncio.sleep(0.1)
    check(alive_in_group(runner.pid) == 0, "сирот от прогона не осталось")

    print("\n21. Проверка, которой нет, не держит заявку вечно")
    stuck = db.create_request("anna", "Заявка ждёт проверку, которая не запускается")
    db.update_request(
        stuck, status=pipeline.CHECKING, head_sha="c0ffee", pr_number=7, checks_status="missing"
    )

    async def fake_missing_state(sha: str) -> dict[str, str]:
        return {"status": "missing", "detail": "проверка «tests» пока не запускалась", "url": ""}

    github.check_state = fake_missing_state  # type: ignore[assignment]
    await pipeline.refresh_checks(db.get_request(stuck))
    check(db.get_request(stuck)["status"] == pipeline.CHECKING, "пока срок не вышел — ждём дальше")
    # Отматываем метку времени назад: ждать в тесте настоящие полчаса незачем.
    connection = db.connect()
    connection.execute("UPDATE requests SET updated_at = ? WHERE id = ?", ("2020-01-01T00:00:00+00:00", stuck))
    connection.commit()
    await pipeline.refresh_checks(db.get_request(stuck))
    late = db.get_request(stuck)
    check(late["status"] == pipeline.REVIEW, f"заявку вернули человеку (получено: {late['status']})")
    check(
        any("не запустилась" in event["text"] for event in db.list_events(stuck)),
        "человеку объяснили, что решение теперь за ним",
    )

    print("\nВсё прошло.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
