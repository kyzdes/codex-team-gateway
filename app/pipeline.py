"""Жизненный цикл заявки: от текста человека до правки на сайте.

    принята → в работе → [вопрос] → проверка GitHub → готово к подтверждению
            → выкатка → готово

Шлюз не верит агенту на слово: что именно изменилось, считается по git,
проходят ли тесты — по статусу проверки в GitHub, доехало ли до сайта —
по Dokploy или health-адресу.

Всё, что пришло из проекта клиента (код после правок агента, его тестовая
команда), запускается только через codex_runner.agent_command + clean_env:
от имени пользователя агента и без секретов шлюза.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import auth, bus, codex_runner, db, deploy, github, gitops, uploads
from .config import settings

logger = logging.getLogger(__name__)

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
# Отменять уже отменённое безвредно, а вот доделанное — нет.
CLOSED_FOR_GOOD = FINAL - {CANCELLED}
# Статусы, после которых заявка никуда не поедет и рабочая копия не нужна.
# needs_input и checking сюда не входят: туда агент ещё вернётся.
SETTLED = FINAL | {TESTS_FAILED}
# Когда имеет смысл спрашивать GitHub про проверки: сторож ходит по checking,
# а кнопка «Проверить заново» нужна ещё и после красного CI (проверку могли
# перезапустить руками). Всё остальное — либо ещё не дошло до PR, либо уже
# закончилось, и «переспросить» там значит воскресить завершённую заявку.
RECHECKABLE = {CHECKING, TESTS_FAILED}

# Подписи для интерфейса живут здесь, а не во фронтенде: иначе при каждом
# новом статусе приходится помнить про два файла в разных языках.
STEPS = ["Принята", "В работе", "Проверка", "Подтверждение", "Выкатка", "Готово"]

STATUS_META: dict[str, dict[str, Any]] = {
    QUEUED: {"label": "В очереди", "tone": "default", "step": 0, "busy": False},
    WORKING: {"label": "В работе", "tone": "accent", "step": 1, "busy": True},
    NEEDS_INPUT: {"label": "Нужен ваш ответ", "tone": "warning", "step": 1, "busy": False},
    CHECKING: {"label": "Идёт проверка", "tone": "accent", "step": 2, "busy": True},
    TESTS_FAILED: {"label": "Проверка не прошла", "tone": "danger", "step": 2, "busy": False},
    REVIEW: {"label": "Ждёт подтверждения", "tone": "warning", "step": 3, "busy": False},
    MERGING: {"label": "Применяю", "tone": "accent", "step": 4, "busy": True},
    DEPLOYING: {"label": "Выкатываю на сайт", "tone": "accent", "step": 4, "busy": True},
    DONE: {"label": "Готово", "tone": "success", "step": 5, "busy": False},
    NO_CHANGES: {"label": "Без изменений", "tone": "default", "step": 5, "busy": False},
    FAILED: {"label": "Ошибка", "tone": "danger", "step": -1, "busy": False},
    CANCELLED: {"label": "Отменена", "tone": "default", "step": -1, "busy": False},
}

_semaphore: asyncio.Semaphore | None = None
_tasks: set[asyncio.Task] = set()
# Заявки, для которых прогон уже поставлен в работу. Задача может простоять в
# очереди цикла событий сколько угодно, и всё это время заявка ещё «queued» —
# без этой отметки resume_queued() поднял бы её второй раз, а два агента в
# одной рабочей копии затирают друг друга (и стоят вдвое).
_in_flight: set[int] = set()
# Последняя жалоба сторожа по заявке: он ходит по кругу каждые несколько
# секунд, и без этого лежащий GitHub залил бы всю ленту одинаковыми строчками.
_last_complaint: dict[int, str] = {}


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


def _spawn(request_id: int, what: str, coro: Any) -> None:
    task = asyncio.create_task(_guarded(request_id, what, coro))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


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


IMAGES_NOTE = (
    "\n\nК сообщению приложены картинки — почти всегда это скриншоты сайта, "
    "сделанные самим сотрудником. Считай их основным описанием проблемы: то, "
    "что человек не сумел объяснить словами, обычно видно на них."
)


def build_prompt(request: dict[str, Any], branch: str, has_images: bool = False) -> str:
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

В summary пиши так, будто объясняешь коллеге из отдела продаж.{IMAGES_NOTE if has_images else ""}"""


def build_followup_prompt(request: dict[str, Any], answer: str, has_images: bool = False) -> str:
    return f"""{auth.display_name(request['user'])} ответил на твой вопрос:
<<<
{answer.strip()}
>>>

Продолжай заявку по тем же правилам: правки в этой же рабочей копии, локальный коммит, никакого пуша. Последнее сообщение — снова один JSON-объект того же формата.{IMAGES_NOTE if has_images else ""}"""


# --- пауза приёма ---------------------------------------------------------

def paused() -> bool:
    """Рантайм-переключатель админа: закрыть приём, не трогая процесс."""
    return db.get_setting("intake_paused", "") == "1"


def resume_queued() -> int:
    """Снятие паузы: всё, что копилось в очереди, уходит в работу.

    Возвращает число поднятых заявок. Те, у кого прогон уже стоит в цикле
    событий и просто не успел начаться, пропускаем — иначе снятие паузы
    запустило бы им второго агента поверх первого.
    """
    if paused():
        return 0
    started = 0
    for request in db.requests_in_statuses([QUEUED]):
        if request["id"] in _in_flight:
            continue
        start(request["id"])
        started += 1
    return started


async def _hold_until_resume(request_id: int) -> None:
    """Пауза: агента не запускаем, заявка ждёт своего часа в очереди.

    Ответ человека при этом не теряется: start() уже вклеил его в текст
    заявки, поэтому resume_queued() поднимет её со всем контекстом.
    """
    set_status(request_id, QUEUED, question="")
    await emit(request_id, "system", "Приём заявок приостановлен — заявка ждёт в очереди")


# --- рабочая копия --------------------------------------------------------

async def _finalize_workspace(request_id: int) -> None:
    """Снять рабочую копию, когда заявка окончательно остановилась.

    В needs_input и checking трогать нельзя: туда агент ещё вернётся тем же
    тредом. Во всех остальных финалах копия только занимает место в томе.
    """
    request = db.get_request(request_id)
    if not request or request["status"] not in SETTLED:
        return
    _last_complaint.pop(request_id, None)
    try:
        await gitops.remove_worktree(request_id)
    except gitops.GitError as exc:
        logger.warning("Заявка %s: рабочая копия не снялась: %s", request_id, exc)


async def _release_pull_request(request: dict[str, Any]) -> None:
    """Закрыть PR и убрать ветку из origin: у заявки, которую человек снял,
    не должно оставаться открытого PR — иначе его однажды смержат «за компанию»."""
    request_id = request["id"]
    if request.get("pr_number"):
        try:
            await github.close_pull_request(request["pr_number"])
        except github.GitHubError as exc:
            logger.warning("Заявка %s: PR не закрылся: %s", request_id, exc)
    await _drop_remote_branch(request)


async def _drop_remote_branch(request: dict[str, Any]) -> None:
    if not request.get("branch"):
        return
    try:
        await gitops.delete_remote_branch(request["branch"])
    except gitops.GitError as exc:
        logger.warning("Заявка %s: ветка не удалилась: %s", request["id"], exc)


# --- шаги пайплайна -------------------------------------------------------

def _log_path(request_id: int) -> Path:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(settings.logs_dir.glob(f"req-{request_id}-*.jsonl")))
    return settings.logs_dir / f"req-{request_id}-{existing + 1}.jsonl"


async def _run_tests(path: Path, request_id: int) -> tuple[str, str]:
    """Прогон тестовой команды клиента. Возвращает (ok|failed|skipped, вывод).

    Команду задаёт клиент, а исполняется она поверх кода, который агент только
    что правил, — то есть агент может выполнить через неё что угодно. Поэтому
    запускаем ровно так же, как Codex: от пользователя агента и с окружением
    без секретов шлюза. Раньше она шла от пользователя шлюза, у которого в
    окружении лежат GitHub-токен и токены доступа всех сотрудников.
    """
    if not settings.test_cmd:
        return "skipped", ""
    ready, reason = codex_runner.isolation()
    if not ready and not settings.allow_unisolated_agent:
        # Запускать код, который агент только что правил, от пользователя шлюза
        # опаснее всего: у него в окружении GitHub-токен и токены сотрудников.
        # Лучше не проверить вовсе, чем проверить такой ценой.
        logger.warning("Тестовая команда проекта не запущена: %s", reason)
        await emit(request_id, "system", "Проверка проекта пропущена — так настроен шлюз")
        return "skipped", ""
    await emit(request_id, "progress", "Проверяю, что ничего не сломалось")
    proc = await asyncio.create_subprocess_exec(
        *codex_runner.agent_command(["bash", "-lc", settings.test_cmd]),
        cwd=str(path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=codex_runner.clean_env(),
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.test_timeout)
    except asyncio.TimeoutError:
        # Команда идёт от пользователя агента, поэтому прямой kill получил бы
        # EPERM: вместо контрактного «не уложились» заявка падала с
        # PermissionError, а зависшая команда оставалась сиротой в рабочей копии.
        await codex_runner.kill_group(proc)
        return "failed", "Тесты не уложились во время"
    tail = out.decode("utf-8", errors="replace")[-4000:]
    return ("ok" if proc.returncode == 0 else "failed"), tail


def _usage_of(result: codex_runner.CodexResult) -> dict[str, int]:
    """Расход одного прогона. Складывать с прошлыми — задача базы, поэтому
    turns здесь всегда 1: заявка с тремя уточнениями стоила три прогона."""
    usage: dict[str, int] = {"turns": 1}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        value = (result.usage or {}).get(key)
        if isinstance(value, (int, float)):
            usage[key] = int(value)
    return usage


def _agent_fields(
    request: dict[str, Any], result: codex_runner.CodexResult, parsed: dict[str, Any]
) -> dict[str, Any]:
    """То, что агент рассказал о себе. Факты об изменениях добираются из git."""
    lines = request["body"].strip().splitlines()
    fallback = request.get("title") or (lines[0][:120] if lines else f"Заявка #{request['id']}")
    return {
        "thread_id": result.thread_id,
        "title": (parsed.get("title") or "").strip()[:120] or fallback,
        "summary": (parsed.get("summary") or "").strip(),
        "user_visible": parsed.get("user_visible") or [],
        "notes": (parsed.get("notes") or "").strip(),
        "risk": parsed.get("risk") or "low",
    }


async def _fail(request_id: int, error: str, feed: str, fields: dict[str, Any] | None = None) -> None:
    set_status(request_id, FAILED, error=error, **(fields or {}))
    await emit(request_id, "error", feed)


async def _ask_user(request_id: int, question: str, fields: dict[str, Any]) -> None:
    set_status(request_id, NEEDS_INPUT, question=question, **fields)
    await emit(request_id, "agent", question)


async def _close_without_changes(request_id: int, fields: dict[str, Any], feed: str) -> None:
    set_status(request_id, NO_CHANGES, question="", **fields)
    await emit(request_id, "system", feed)


async def _collect_facts(path: Path, branch: str, request_id: int, title: str) -> gitops.Facts:
    """Подстраховка: агент мог забыть закоммитить — тогда коммитим за него.

    Дальше факты считаются уже по ветке в копии шлюза, а не по рабочей копии:
    её содержимое, включая настройки git, правит агент.
    """
    if await gitops.has_uncommitted(path):
        await gitops.commit_all(path, title or f"Заявка #{request_id}")
    return await gitops.collect_facts(branch)


async def _publish_for_review(
    request_id: int, request: dict[str, Any], path: Path, branch: str, fields: dict[str, Any]
) -> None:
    """Последний отрезок: тесты → пуш → PR → ожидание проверок GitHub."""
    verdict, output = await _run_tests(path, request_id)
    fields["tests_local"] = verdict
    if verdict == "failed":
        fields["tests_output"] = output
        set_status(request_id, TESTS_FAILED, error="Автоматическая проверка проекта не прошла", **fields)
        await emit(request_id, "error", "Тесты проекта не прошли — правка не отправлена")
        return

    # Факты о правке сохраняем до отправки: если пуш или PR сорвутся, человек
    # всё равно должен видеть, что именно агент сделал, а не пустую карточку.
    db.update_request(request_id, **fields)

    await emit(request_id, "progress", "Отправляю правку на проверку")
    await gitops.push_branch(branch)

    title = fields["title"] or f"Заявка #{request_id}"
    body = (
        f"Заявка #{request_id} от {auth.display_name(request['user'])}\n\n"
        f"**Просьба:**\n> {request['body'].strip()}\n\n"
        f"**Что сделано:** {fields['summary'] or '—'}\n\n"
        f"_Открыто автоматически через {settings.brand_name}._"
    )
    try:
        pr = await github.create_pull_request(branch, title, body)
    except github.GitHubError:
        # Ветка могла уехать в PR на прошлой попытке — второй заводить не нужно.
        pr = await github.find_open_pull_request(branch)
        if not pr:
            raise
    fields.update({"pr_number": pr["number"], "pr_url": pr["url"], "head_sha": pr["head_sha"]})
    set_status(request_id, CHECKING, checks_status="pending", **fields)
    await emit(request_id, "system", "Правка готова, идёт автоматическая проверка")


async def _handle_agent_result(
    request_id: int, result: codex_runner.CodexResult, path: Path, branch: str
) -> None:
    """Куда заявке двигаться после прогона: сам разбор ответа агента.

    Веток выхода шесть, поэтому каждая — отдельная функция: иначе не видно,
    что из них терминальные, а что оставляет заявку в работе.
    """
    request = db.get_request(request_id)
    if not request or request["status"] == CANCELLED:
        return  # человек отменил заявку, пока агент дорабатывал

    if result.timed_out:
        await _fail(
            request_id,
            "Агент не уложился в отведённое время. Попробуйте разбить заявку на части.",
            "Работа прервана по таймауту",
        )
        return

    parsed = codex_runner.parse_result_json(result.final_text) or {}
    fields = _agent_fields(request, result, parsed)
    outcome = parsed.get("status")

    if outcome == "question" and parsed.get("question"):
        await _ask_user(request_id, parsed["question"].strip(), fields)
        return

    if result.exit_code != 0 and not parsed:
        detail = result.stderr_tail[-500:] or f"codex завершился с кодом {result.exit_code}"
        await _fail(request_id, detail, "Агент завершился с ошибкой", fields)
        return

    if outcome == "impossible":
        await _close_without_changes(request_id, fields, "Агент считает, что менять ничего не нужно")
        return

    facts = await _collect_facts(path, branch, request_id, fields["title"])
    if facts.commits == 0:
        fields["summary"] = fields["summary"] or "Изменений в проекте не появилось."
        await _close_without_changes(request_id, fields, "Файлы проекта не изменились")
        return

    fields.update(
        {
            "files": [f"{f['status']} {f['path']}" for f in facts.files],
            "text_changes": facts.text_changes,
            "head_sha": facts.head_sha,
            "branch": branch,
        }
    )
    await _publish_for_review(request_id, request, path, branch, fields)


# Технические отказы, которые человек увидит в карточке. Слева — примета в
# тексте ошибки, справа — то, что сотруднику реально поможет.
HUMAN_ERRORS: tuple[tuple[str, str], ...] = (
    (
        "could not read Username",
        "Шлюзу не выдан доступ к GitHub — правка сделана, но отправить её некуда. "
        "Сообщите администратору: нужен ключ GitHub.",
    ),
    (
        "Authentication failed",
        "GitHub не принял доступ шлюза. Сообщите администратору: токен истёк или отозван.",
    ),
    ("GITHUB_TOKEN не задан", "Шлюзу не выдан доступ к GitHub. Сообщите администратору."),
)


def humanize(message: str) -> str:
    """Показать нетехническому человеку причину, а не вывод git.

    Полный технический текст никуда не девается: он остаётся в ленте события
    и в логе прогона, куда смотрит администратор.
    """
    for marker, human in HUMAN_ERRORS:
        if marker in message:
            return human
    return message


async def _guarded(request_id: int, what: str, coro: Any) -> None:
    try:
        await coro
    except (gitops.GitError, github.GitHubError) as exc:
        logger.warning("Заявка %s: %s — %s", request_id, what, exc)
        set_status(request_id, FAILED, error=humanize(str(exc)))
        await emit(request_id, "error", f"{what}: {exc}")
    except Exception as exc:  # noqa: BLE001 — заявка не должна падать молча
        logger.exception("Заявка %s: «%s» — внутренняя ошибка", request_id, what)
        set_status(request_id, FAILED, error=f"{exc.__class__.__name__}: {exc}")
        await emit(request_id, "error", f"Внутренняя ошибка: {exc}")
    finally:
        # Единственное место, где решается судьба рабочей копии: сюда заявка
        # приходит с любым финальным статусом, включая выставленный обработчиком
        # ошибки выше. Сама функция молча уходит, если заявка ещё в работе.
        await _finalize_workspace(request_id)


async def _process(request_id: int, followup: str | None, images: list[str] | None = None) -> None:
    request = db.get_request(request_id)
    if not request or request["status"] == CANCELLED:
        return
    if paused():
        await _hold_until_resume(request_id)
        return

    async with semaphore():
        # Ожидание слота бывает долгим: за это время заявку могли отменить,
        # а приём — закрыть. И то и другое сильнее нашего места в очереди.
        request = db.get_request(request_id)
        if not request or request["status"] == CANCELLED:
            return
        if paused():
            await _hold_until_resume(request_id)
            return
        set_status(request_id, WORKING, error="", question="")
        await emit(request_id, "system", "Взял заявку в работу" if not followup else "Продолжаю с учётом ответа")

        # На первом прогоне отдаём агенту все картинки заявки, на следующих —
        # только новые: старые он уже видел в этом же треде.
        turn_images = images if followup else (images or request.get("images") or [])
        image_paths = uploads.paths_for(request_id, turn_images or [])

        await gitops.ensure_repo()
        if followup and request.get("branch") and gitops.worktree_path(request_id).exists():
            path = gitops.worktree_path(request_id)
            branch = request["branch"]
            prompt = build_followup_prompt(request, followup, bool(image_paths))
            thread_id = request.get("thread_id")
        else:
            path, branch = await gitops.create_worktree(request_id)
            # Рабочей копии может уже не быть (перезапуск сервиса) — начинаем
            # с нуля. Ответ человека при этом на месте: start() вклеил его
            # в текст заявки. Заново — значит и со всеми её картинками.
            image_paths = uploads.paths_for(request_id, request.get("images") or [])
            prompt = build_prompt(request, branch, bool(image_paths))
            thread_id = None
            db.update_request(request_id, branch=branch)

        async def on_progress(kind: str, text: str) -> None:
            await emit(request_id, kind, text)

        result = await codex_runner.run(
            request_id, prompt, path, thread_id, _log_path(request_id), on_progress, image_paths
        )
        # Токены сгорели независимо от того, чем кончился прогон.
        db.add_usage(request_id, _usage_of(result))
        await _handle_agent_result(request_id, result, path, branch)


async def _released(request_id: int, coro: Any) -> None:
    """Снять отметку «уже в работе» ровно там, где прогон закончился."""
    try:
        await coro
    finally:
        _in_flight.discard(request_id)


def start(request_id: int, followup: str | None = None, images: list[str] | None = None) -> None:
    """Поставить заявку в работу.

    Ответ человека сразу вклеиваем в текст заявки: между этим вызовом и
    началом прогона заявка может простоять в очереди сколько угодно — из-за
    занятых слотов, паузы приёма или перезапуска сервиса. Всё это время ответ
    живёт только в замыкании фоновой задачи, а resume_queued() и восстановление
    после перезапуска о нём ничего не знают.

    Повторный вызов для той же заявки не делает ничего: прогон агента стоит
    денег, а два прогона в одной рабочей копии ещё и затирают друг друга.
    """
    if request_id in _in_flight:
        return
    if followup:
        request = db.get_request(request_id)
        if request:
            db.update_request(
                request_id,
                body=f"{request['body'].strip()}\n\nУточнение от сотрудника: {followup.strip()}",
            )
    _in_flight.add(request_id)
    work = _released(request_id, _process(request_id, followup, images))
    _spawn(request_id, "Обработка заявки", work)


# --- подтверждение и выкатка ---------------------------------------------

async def _after_merge(request_id: int, request: dict[str, Any]) -> None:
    """Общий хвост обоих путей мержа — кнопкой в шлюзе и руками на GitHub.

    Оба пути сходятся на одной заявке легко: человек нажал «Выкатить», а сторож
    на прошлом круге уже увидел смерженный PR. Поэтому статус перечитываем и
    занимаем его до первого await — иначе выкатка шла бы дважды, второй
    wait_for_deploy висел бы до таймаута и в конце ставил «Ошибка» заявке,
    которая давно доехала.
    """
    fresh = db.get_request(request_id)
    if not fresh or fresh["status"] not in {REVIEW, MERGING}:
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

    # Содержимое ветки уже в основной версии — держать её в origin незачем.
    await _drop_remote_branch(request)
    await _finalize_workspace(request_id)


async def _approve(request_id: int, approved_by: str = "") -> None:
    """Пустой approved_by значит «мерж пришёл не через кнопку» — тогда журнал
    выкаток не трогаем, там останется то, что записал вызывающий."""
    request = db.get_request(request_id)
    # main.py переводит заявку в merging синхронно, чтобы поймать двойное
    # нажатие 409-м, — поэтому сюда она приезжает и в review, и уже в merging.
    if not request or request["status"] not in {REVIEW, MERGING} or not request.get("pr_number"):
        return

    journal: dict[str, Any] = {}
    if approved_by:
        journal = {"approved_by": approved_by, "approved_at": request.get("approved_at") or db.now()}
    set_status(request_id, MERGING, error="", **journal)
    await emit(request_id, "system", "Подтверждено, отправляю в основную версию сайта")

    try:
        await github.merge(request["pr_number"], request.get("title") or f"Заявка #{request_id}")
    except github.GitHubError as exc:
        # Мержа не было: заявка возвращается к человеку, а не падает в ошибку.
        set_status(request_id, REVIEW, error=f"GitHub не принял изменение: {exc}")
        await emit(request_id, "error", f"Не удалось применить: {exc}")
        return

    await _after_merge(request_id, request)


def approve(request_id: int, approved_by: str) -> None:
    _spawn(request_id, "Подтверждение", _approve(request_id, approved_by))


async def cancel(request_id: int) -> None:
    request = db.get_request(request_id)
    # main.py может пометить заявку отменённой синхронно (защита от двойного
    # нажатия), поэтому статус CANCELLED здесь не повод ничего не делать:
    # PR и рабочая копия всё равно ждут уборки.
    if not request or request["status"] in CLOSED_FOR_GOOD:
        return
    await _release_pull_request(request)
    set_status(request_id, CANCELLED)
    await emit(request_id, "system", "Заявка отменена")
    await _finalize_workspace(request_id)


async def force_cancel(request_id: int) -> None:
    """Аварийная кнопка администратора: снять заявку, даже если агент завис.

    Обычная отмена ждёт, пока прогон сам заметит новый статус, — а зависший
    прогон не заметит его никогда, потому что стоит на своей команде.
    """
    request = db.get_request(request_id)
    if not request or request["status"] in CLOSED_FOR_GOOD:
        return
    killed = await codex_runner.terminate(request_id)
    await _release_pull_request(request)
    set_status(request_id, CANCELLED, error="Заявка снята администратором", question="")
    await emit(
        request_id,
        "system",
        "Заявка снята принудительно, прогон агента остановлен" if killed
        else "Заявка снята принудительно",
    )
    await _finalize_workspace(request_id)


# --- фоновый опрос проверок GitHub ---------------------------------------

STAGING_SWEEP_EVERY = 30  # тиков между уборками забытых черновиков
HOUSEKEEPING_SECONDS = 3600  # ретеншен смотрим раз в час
# Сколько ждём обязательную проверку, прежде чем признать, что она не придёт.
CHECKS_DEADLINE = 30 * 60
_LOG_FILE = re.compile(r"^req-(\d+)-\d+\.jsonl$")

Step = Callable[[dict[str, Any]], Awaitable[None]]


async def checks_watcher(interval: int = 20) -> None:
    """Сторож: проверки GitHub, ручные мержи и уборка по сроку хранения.

    Умирать ему нельзя, но и молчать тоже: раньше любая ошибка тут исчезала
    без следа, заявка навсегда оставалась в «идёт проверка», а человек
    смотрел на крутилку и не понимал, чего ждёт.
    """
    tick = 0
    housekeeping_every = max(1, HOUSEKEEPING_SECONDS // max(1, interval))
    while True:
        tick += 1
        try:
            for request in db.requests_in_statuses([CHECKING]):
                await _watch(request, refresh_checks, "Опрос проверок GitHub")
            if tick % 3 == 0:
                for request in db.requests_in_statuses([REVIEW]):
                    await _watch(request, _detect_external_merge, "Проверка ручного мержа")
            # Уборка ходит по файловой системе, а сторож живёт в том же цикле,
            # что и SSE-ленты всех открытых вкладок: подвешивать его нельзя.
            if tick % STAGING_SWEEP_EVERY == 0:
                await asyncio.to_thread(uploads.sweep_staging)
            if tick % housekeeping_every == 0:
                await asyncio.to_thread(_housekeeping)
        except Exception:  # noqa: BLE001 — сторож не должен умирать вместе с одной заявкой
            logger.exception("Сторож проверок споткнулся")
        await asyncio.sleep(interval)


async def _watch(request: dict[str, Any], step: Step, what: str) -> None:
    """Один шаг сторожа по одной заявке: ошибка не прерывает обход и не молчит."""
    request_id = request["id"]
    try:
        await step(request)
    except Exception as exc:  # noqa: BLE001 — обход продолжается со следующей заявки
        expected = isinstance(exc, (github.GitHubError, gitops.GitError))
        if expected:
            logger.warning("Заявка %s: %s — %s", request_id, what, exc)
            complaint = f"{what}: {exc}"
        else:
            logger.exception("Заявка %s: «%s» — внутренняя ошибка", request_id, what)
            complaint = f"{what}: внутренняя ошибка ({exc.__class__.__name__})"
        if _last_complaint.get(request_id) != complaint:
            _last_complaint[request_id] = complaint
            await emit(request_id, "error", complaint)
        return
    _last_complaint.pop(request_id, None)


async def _detect_external_merge(request: dict[str, Any]) -> None:
    """PR могли смержить руками на GitHub — заявка не должна зависнуть."""
    if not request.get("pr_number"):
        return
    state = await github.pull_request_state(request["pr_number"])
    if not state["merged"]:
        return
    # Снимок сторожа устарел на время запроса к GitHub: пока мы спрашивали,
    # человек мог отменить заявку или нажать «Выкатить» сам.
    fresh = db.get_request(request["id"])
    if not fresh or fresh["status"] != REVIEW:
        return
    # Выкатку ждём фоном: она длится до DEPLOY_TIMEOUT, а сторож всё это время
    # обязан продолжать обход — иначе соседние заявки стоят в «идёт проверка»
    # уже после того, как GitHub дал зелёный.
    _spawn(request["id"], "Выкатка", _after_merge(request["id"], fresh))


def _stalled_for(request: dict[str, Any]) -> float:
    """Сколько секунд заявка стоит без единого изменения."""
    try:
        marked = datetime.fromisoformat(str(request.get("updated_at") or ""))
    except ValueError:
        return 0.0
    if marked.tzinfo is None:
        marked = marked.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - marked).total_seconds()


def _check_never_came(request: dict[str, Any], state: dict[str, str]) -> bool:
    """Проверка не появилась и, судя по всему, не появится.

    «missing» — это не «идёт», а «на этом коммите такого чека нет»: workflow не
    срабатывает на ветках шлюза или GITHUB_REQUIRED_CHECK не совпал с именем
    проверки. Медленный CI сюда не попадает, он отвечает «pending».
    """
    return (
        request["status"] == CHECKING
        and state["status"] == "missing"
        and _stalled_for(request) > CHECKS_DEADLINE
    )


async def refresh_checks(request: dict[str, Any]) -> None:
    """Спросить у GitHub, чем кончилась проверка, и подвинуть заявку.

    Публичная: сюда ходит и сторож по кругу, и кнопка «Проверить заново» в
    админке. Переходы статусов обязаны быть в одном месте — иначе кнопка и
    сторож однажды разойдутся в том, что считать провалом.

    Оба зовущих приносят снимок, который успевает устареть: сторож взял его до
    похода в сеть, админка — когда рисовала карточку. Поэтому статус читаем
    заново, иначе кнопка возвращала бы в review уже выкаченную или только что
    отменённую заявку.
    """
    request_id = request["id"]
    fresh = db.get_request(request_id)
    if not fresh or fresh["status"] not in RECHECKABLE:
        return
    request = fresh
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
        # Статус терминальный, а шаг сторожа не проходит через _guarded —
        # значит рабочую копию, кроме нас, здесь снять некому.
        await _finalize_workspace(request_id)
    elif state["status"] != request.get("checks_status"):
        # Через set_status, а не db.update_request: иначе открытая вкладка не
        # узнает о сдвиге проверки и останется на прежней подписи.
        set_status(request_id, request["status"], checks_status=state["status"],
                   checks_detail=state["detail"])
    elif _check_never_came(request, state):
        # Оставлять заявку в «идёт проверка» навсегда нельзя: человек смотрит
        # на крутилку и не понимает, чего ждёт, а сторож при этом молчит.
        set_status(request_id, REVIEW, checks_status=state["status"], checks_detail=state["detail"])
        await emit(
            request_id,
            "system",
            "Автоматическая проверка так и не запустилась. Решать вам: "
            "правку можно выкатить или отменить заявку.",
        )


# --- срок хранения --------------------------------------------------------

def _moving_requests() -> set[int]:
    """Заявки, которые ещё двигаются. Их лог и их картинки могут понадобиться
    прямо сейчас, каким бы старым ни был файл: заявка неделю ждёт ответа
    человека, а после ответа прогон пойдёт заново и с теми же скриншотами."""
    return {
        request["id"]
        for request in db.requests_in_statuses(sorted(ACTIVE | {NEEDS_INPUT, CHECKING, REVIEW}))
    }


def _purge_logs(days: int, moving: set[int]) -> int:
    """Логи прогонов — диагностика, а не документ: держим их ровно столько,
    сколько разрешил клиент."""
    deadline = time.time() - days * 86400
    removed = 0
    for path in settings.logs_dir.glob("req-*.jsonl"):
        match = _LOG_FILE.match(path.name)
        if not match or int(match.group(1)) in moving:
            continue
        try:
            if path.stat().st_mtime < deadline:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _housekeeping() -> None:
    """Ретеншен: том не должен расти до конца времён."""
    days = settings.retention_days
    if days <= 0:
        return
    moving = _moving_requests()
    logs = _purge_logs(days, moving)
    images = uploads.purge_old(days, moving)
    if logs or images:
        logger.info("Уборка по сроку %s дн.: логов %s, папок с картинками %s", days, logs, images)


async def recover_after_restart() -> None:
    """Заявки, которые шли в момент перезапуска, нельзя оставить «висящими»."""
    for request in db.requests_in_statuses([WORKING, MERGING, DEPLOYING]):
        request_id = request["id"]
        set_status(
            request_id,
            FAILED,
            error="Работа прервана перезапуском сервиса. Отправьте заявку заново.",
        )
        await emit(request_id, "error", "Сервис был перезапущен, заявка прервана")
        await _finalize_workspace(request_id)
    # Очередь перезапуск переживает: работа по ней ещё не начиналась, терять
    # её незачем. Если приём на паузе, resume_queued() сам ничего не тронет.
    resume_queued()
