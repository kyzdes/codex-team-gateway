"""Жизненный цикл заявки: от текста человека до правки на сайте.

    принята → в работе → [вопрос] → проверка GitHub → готово к подтверждению
            → выкатка → готово

Шлюз не верит агенту на слово: что именно изменилось, считается по git,
проходят ли тесты — по статусу проверки в GitHub, доехало ли до сайта —
по Dokploy или health-адресу.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from . import auth, bus, codex_runner, db, deploy, github, gitops
from .config import settings

# --- статусы --------------------------------------------------------------
QUEUED = "queued"
WORKING = "working"
NEEDS_INPUT = "needs_input"
NO_CHANGES = "no_changes"
TESTS_FAILED = "tests_failed"
CHECKING = "checking"
REVIEW = "review"
MERGING = "merging"
DEPLOYING = "deploying"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

ACTIVE = {QUEUED, WORKING, MERGING, DEPLOYING}
FINAL = {DONE, FAILED, CANCELLED, NO_CHANGES}

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()


def semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.max_concurrent))
    return _semaphore


# --- события --------------------------------------------------------------

async def emit(request_id: int, kind: str, text: str) -> None:
    event = db.add_event(request_id, kind, text)
    owner = (db.get_request(request_id) or {}).get("user")
    bus.publish({"type": "event", "user": owner, **event})


def set_status(request_id: int, status: str, **fields: Any) -> None:
    db.update_request(request_id, status=status, **fields)
    request = db.get_request(request_id)
    if request:
        bus.publish({"type": "request", "request": request})


# --- промпт ---------------------------------------------------------------

RESULT_SCHEMA = """{
  "status": "done" | "question" | "impossible",
  "question": "один короткий вопрос, если status = question",
  "title": "заголовок заявки, 3-6 слов",
  "summary": "1-3 предложения простыми словами: что изменилось и где это увидеть",
  "user_visible": ["что заметит обычный посетитель сайта"],
  "risk": "low" | "medium",
  "notes": "что стоит знать перед выкаткой, иначе пустая строка"
}"""


def build_prompt(request: dict[str, Any], branch: str) -> str:
    if settings.test_cmd:
        tests = f"Обязательно прогони `{settings.test_cmd}` и добейся, чтобы всё проходило."
    else:
        tests = (
            "Автоматические тесты запустит CI после тебя. Если в проекте есть быстрый "
            "способ проверить себя (линтер, локальный запуск) — воспользуйся им."
        )
    return f"""Ты выполняешь заявку от нетехнического сотрудника компании. Он не знает, что такое коммит, ветка или деплой, — и не должен узнать.

ЗАЯВКА от {auth.display_name(request['user'])}:
<<<
{request['body'].strip()}
>>>

Правила работы:
1. Ты в отдельной рабочей копии на ветке {branch}, созданной от {settings.base_branch}. Меняй только файлы этого проекта.
2. Сначала прочитай AGENTS.md в корне проекта, если он есть. Правила проекта важнее этой инструкции во всём, что касается «чего нельзя трогать».
3. Ничего не пушь и не трогай origin: `git push`, `gh`, работа с удалёнными ветками запрещены. Отправку сделает система после тебя.
4. Свои изменения обязательно закоммить локально: `git add` нужных файлов и `git commit` с осмысленным сообщением на русском.
5. {tests}
6. Меняй только то, что относится к заявке. Не переформатируй чужой код, не обновляй зависимости, не трогай миграции и рабочие данные, если заявка не об этом.
7. Если заявку можно понять двумя разными способами — не угадывай. Верни status "question" и задай ОДИН короткий вопрос на языке заявки, без технических слов.
8. Если сделать это нельзя или не нужно — верни status "impossible" и объясни в summary простыми словами почему.

Формат ответа: твоё последнее сообщение должно быть одним JSON-объектом, без markdown-обёртки и без пояснений вокруг:
{RESULT_SCHEMA}

В summary пиши так, будто объясняешь коллеге из отдела продаж."""


def build_followup_prompt(request: dict[str, Any], answer: str) -> str:
    return f"""{auth.display_name(request['user'])} ответил на твой вопрос:
<<<
{answer.strip()}
>>>

Продолжай заявку по тем же правилам: правки в этой же рабочей копии, локальный коммит, никакого пуша. Последнее сообщение — снова один JSON-объект того же формата."""


# --- шаги пайплайна -------------------------------------------------------

def _log_path(request_id: int) -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(settings.logs_dir.glob(f"req-{request_id}-*.jsonl")))
    return settings.logs_dir / f"req-{request_id}-{existing + 1}.jsonl"


async def _run_tests(path: Path, request_id: int) -> tuple[bool, str]:
    if not settings.test_cmd:
        return True, "skipped"
    await emit(request_id, "progress", "Проверяю, что ничего не сломалось")
    proc = await asyncio.create_subprocess_shell(
        settings.test_cmd,
        cwd=str(path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.test_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "Тесты не уложились во время"
    tail = out.decode("utf-8", errors="replace")[-4000:]
    return proc.returncode == 0, tail


async def _handle_agent_result(request_id: int, result: codex_runner.CodexResult, path: Path, branch: str) -> None:
    request = db.get_request(request_id)
    if not request:
        return
    if request["status"] == CANCELLED:
        return  # человек отменил заявку, пока агент дорабатывал

    if result.timed_out:
        set_status(request_id, FAILED, error="Агент не уложился в отведённое время. Попробуйте разбить заявку на части.")
        await emit(request_id, "error", "Работа прервана по таймауту")
        return

    parsed = codex_runner.parse_result_json(result.final_text) or {}
    outcome = parsed.get("status")
    title = (parsed.get("title") or "").strip()[:120]
    summary = (parsed.get("summary") or "").strip()
    fields: dict[str, Any] = {
        "thread_id": result.thread_id,
        "title": title or (request.get("title") or request["body"].strip().splitlines()[0][:120]),
        "summary": summary,
        "user_visible": parsed.get("user_visible") or [],
        "notes": (parsed.get("notes") or "").strip(),
        "risk": parsed.get("risk") or "low",
    }

    if outcome == "question" and parsed.get("question"):
        set_status(request_id, NEEDS_INPUT, question=parsed["question"].strip(), **fields)
        await emit(request_id, "agent", parsed["question"].strip())
        return

    if result.exit_code != 0 and not parsed:
        detail = result.stderr_tail[-500:] or f"codex завершился с кодом {result.exit_code}"
        set_status(request_id, FAILED, error=detail, **fields)
        await emit(request_id, "error", "Агент завершился с ошибкой")
        return

    if outcome == "impossible":
        set_status(request_id, NO_CHANGES, question="", **fields)
        await emit(request_id, "system", "Агент считает, что менять ничего не нужно")
        return

    # Подстраховка: агент мог забыть закоммитить.
    if await gitops.has_uncommitted(path):
        await gitops.commit_all(path, fields["title"] or f"Заявка #{request_id}")

    facts = await gitops.collect_facts(path)
    if facts.commits == 0:
        set_status(
            request_id,
            NO_CHANGES,
            question="",
            summary=summary or "Изменений в проекте не появилось.",
            **{k: v for k, v in fields.items() if k != "summary"},
        )
        await emit(request_id, "system", "Файлы проекта не изменились")
        return

    fields.update(
        {
            "files": [f"{f['status']} {f['path']}" for f in facts.files],
            "text_changes": facts.text_changes,
            "head_sha": facts.head_sha,
            "branch": branch,
        }
    )

    tests_ok, tests_output = await _run_tests(path, request_id)
    fields["tests_local"] = "skipped" if tests_output == "skipped" else ("ok" if tests_ok else "failed")
    if not tests_ok:
        fields["tests_output"] = tests_output
        set_status(request_id, TESTS_FAILED, error="Автоматическая проверка проекта не прошла", **fields)
        await emit(request_id, "error", "Тесты проекта не прошли — правка не отправлена")
        return

    await emit(request_id, "progress", "Отправляю правку на проверку")
    await gitops.push_branch(path, branch)

    body = (
        f"Заявка #{request_id} от {auth.display_name(request['user'])}\n\n"
        f"**Просьба:**\n> {request['body'].strip()}\n\n"
        f"**Что сделано:** {summary or '—'}\n\n"
        f"_Открыто автоматически через {settings.brand_name}._"
    )
    pr = await github.create_pull_request(branch, fields["title"] or f"Заявка #{request_id}", body)
    fields.update({"pr_number": pr["number"], "pr_url": pr["url"], "head_sha": pr["head_sha"]})
    set_status(request_id, CHECKING, checks_status="pending", **fields)
    await emit(request_id, "system", "Правка готова, идёт автоматическая проверка")


async def _guarded(request_id: int, coro_name: str, coro: Any) -> None:
    try:
        await coro
    except (gitops.GitError, github.GitHubError) as exc:
        set_status(request_id, FAILED, error=str(exc))
        await emit(request_id, "error", f"{coro_name}: {exc}")
    except Exception as exc:  # noqa: BLE001 — заявка не должна падать молча
        set_status(request_id, FAILED, error=f"{exc.__class__.__name__}: {exc}")
        await emit(request_id, "error", f"Внутренняя ошибка: {exc}")


async def _process(request_id: int, followup: str | None) -> None:
    async with semaphore():
        request = db.get_request(request_id)
        if not request or request["status"] == CANCELLED:
            return
        set_status(request_id, WORKING, error="", question="")
        await emit(request_id, "system", "Взял заявку в работу" if not followup else "Продолжаю с учётом ответа")

        await gitops.ensure_repo()
        if followup and request.get("branch") and gitops.worktree_path(request_id).exists():
            path = gitops.worktree_path(request_id)
            branch = request["branch"]
            prompt = build_followup_prompt(request, followup)
            thread_id = request.get("thread_id")
        else:
            path, branch = await gitops.create_worktree(request_id)
            # Рабочей копии может уже не быть (перезапуск сервиса) — тогда
            # начинаем заново, но ответ человека обязаны сохранить.
            enriched = dict(request)
            if followup:
                enriched["body"] = f"{request['body'].strip()}\n\nУточнение от сотрудника: {followup.strip()}"
            prompt = build_prompt(enriched, branch)
            thread_id = None
            db.update_request(request_id, branch=branch)

        async def on_progress(kind: str, text: str) -> None:
            await emit(request_id, kind, text)

        result = await codex_runner.run(prompt, path, thread_id, _log_path(request_id), on_progress)
        await _handle_agent_result(request_id, result, path, branch)


def start(request_id: int, followup: str | None = None) -> None:
    task = asyncio.create_task(_guarded(request_id, "Обработка заявки", _process(request_id, followup)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# --- подтверждение и выкатка ---------------------------------------------

async def _approve(request_id: int) -> None:
    request = db.get_request(request_id)
    if not request or request["status"] != REVIEW or not request.get("pr_number"):
        return
    set_status(request_id, MERGING)
    await emit(request_id, "system", "Подтверждено, отправляю в основную версию сайта")
    try:
        await github.merge(request["pr_number"], request.get("title") or f"Заявка #{request_id}")
    except github.GitHubError as exc:
        set_status(request_id, REVIEW, error=f"GitHub не принял изменение: {exc}")
        await emit(request_id, "error", f"Не удалось применить: {exc}")
        return

    set_status(request_id, DEPLOYING, merged_at=db.now(), error="")
    await emit(request_id, "system", "Изменение принято, идёт выкатка на сайт")

    async def on_progress(kind: str, text: str) -> None:
        await emit(request_id, kind, text)

    ok, detail = await deploy.wait_for_deploy(on_progress)
    if ok:
        set_status(request_id, DONE, deployed_at=db.now(), error="")
        await emit(request_id, "system", detail)
    else:
        set_status(request_id, FAILED, error=detail)
        await emit(request_id, "error", detail)

    await gitops.remove_worktree(request_id)
    if request.get("branch"):
        await gitops.delete_remote_branch(request["branch"])


def approve(request_id: int) -> None:
    task = asyncio.create_task(_guarded(request_id, "Подтверждение", _approve(request_id)))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def cancel(request_id: int) -> None:
    request = db.get_request(request_id)
    if not request or request["status"] in FINAL:
        return
    if request.get("pr_number"):
        try:
            await github.close_pull_request(request["pr_number"])
        except github.GitHubError:
            pass
    if request.get("branch"):
        await gitops.delete_remote_branch(request["branch"])
    await gitops.remove_worktree(request_id)
    set_status(request_id, CANCELLED)
    await emit(request_id, "system", "Заявка отменена")


# --- фоновый опрос проверок GitHub ---------------------------------------

async def checks_watcher(interval: int = 20) -> None:
    tick = 0
    while True:
        try:
            for request in db.requests_in_statuses([CHECKING]):
                await _refresh_checks(request)
            tick += 1
            if tick % 3 == 0:
                for request in db.requests_in_statuses([REVIEW]):
                    await _detect_external_merge(request)
        except Exception:  # noqa: BLE001 — сторож не должен умирать
            pass
        await asyncio.sleep(interval)


async def _detect_external_merge(request: dict[str, Any]) -> None:
    """PR могли смержить руками на GitHub — заявка не должна зависнуть."""
    if not request.get("pr_number"):
        return
    state = await github.pull_request_state(request["pr_number"])
    if not state["merged"]:
        return
    request_id = request["id"]
    set_status(request_id, DEPLOYING, merged_at=db.now())
    await emit(request_id, "system", "Изменение принято, идёт выкатка на сайт")

    async def on_progress(kind: str, text: str) -> None:
        await emit(request_id, kind, text)

    ok, detail = await deploy.wait_for_deploy(on_progress)
    set_status(request_id, DONE if ok else FAILED,
               deployed_at=db.now() if ok else None, error="" if ok else detail)
    await emit(request_id, "system" if ok else "error", detail)
    await gitops.remove_worktree(request_id)


async def _refresh_checks(request: dict[str, Any]) -> None:
    request_id = request["id"]
    if not settings.required_check:
        set_status(request_id, REVIEW, checks_status="success")
        await emit(request_id, "system", "Правка готова к вашему подтверждению")
        return
    if not request.get("head_sha"):
        return

    state = await github.check_state(request["head_sha"])
    if state["status"] == "success":
        set_status(request_id, REVIEW, checks_status="success", checks_detail="")
        await emit(request_id, "system", "Автоматическая проверка прошла. Ждём вашего подтверждения")
    elif state["status"] == "failure":
        set_status(request_id, TESTS_FAILED, checks_status="failure", checks_detail=state["detail"],
                   error="Автоматическая проверка не прошла")
        await emit(request_id, "error", "Автоматическая проверка не прошла")
    elif state["status"] != request.get("checks_status"):
        db.update_request(request_id, checks_status=state["status"], checks_detail=state["detail"])


async def recover_after_restart() -> None:
    """Заявки, которые шли в момент перезапуска, нельзя оставить «висящими»."""
    for request in db.requests_in_statuses([WORKING, MERGING, DEPLOYING, QUEUED]):
        set_status(
            request["id"],
            FAILED,
            error="Работа прервана перезапуском сервиса. Отправьте заявку заново.",
        )
        await emit(request["id"], "error", "Сервис был перезапущен, заявка прервана")


def dump_state() -> str:
    return json.dumps({"active_tasks": len(_tasks)}, ensure_ascii=False)
