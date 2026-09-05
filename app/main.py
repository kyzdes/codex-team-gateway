"""HTTP-слой: заявки, лента событий, админка."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, bus, codex_runner, config, db, deploy, github, gitops, pipeline, uploads
from .config import settings

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Вход по ссылке ?k=… обменивается на HttpOnly-cookie: иначе токен остаётся
# в истории браузера, в закладках и в заголовке Referer у любой внешней ссылки.
SESSION_COOKIE = "gw_session"
SESSION_MAX_AGE = 90 * 24 * 3600

GITHUB_API = "https://api.github.com"
PROBE_TIMEOUT = 20


# Прогрев локальной копии проекта — строго одна задача на процесс: второй
# параллельный клон писал бы в тот же каталог поверх первого.
_warmup: asyncio.Task[None] | None = None


async def warm_repo() -> None:
    """Клон проекта занимает минуты — тянем его в фоне, чтобы старт не ждал.

    Упавший клон сервис не роняет: администратор увидит это пунктом
    «Локальная копия проекта» в чек-листе готовности.
    """
    try:
        await gitops.ensure_repo()
    except Exception as exc:  # noqa: BLE001 — сервис поднимается и без копии
        logger.warning("Копия проекта не подготовилась: %s", exc)


def warm_repo_in_background() -> None:
    """Поставить прогрев копии в фон, если он не идёт прямо сейчас.

    Зовётся не только на старте, но и сразу после того, как администратор
    вписал ключ GitHub. На свежем инстансе стартовый клон приватного
    репозитория падает из-за отсутствия доступа, и без повторной попытки
    пункт «Локальная копия проекта» оставался бы красным до перезапуска —
    то есть до ровно того редеплоя, ради отмены которого форма и делалась.
    """
    global _warmup
    if _warmup is not None and not _warmup.done():
        return
    _warmup = asyncio.create_task(warm_repo())


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.connect()
    # Люди живут в базе, поэтому завести их можно только после connect().
    auth.seed_people()
    await pipeline.recover_after_restart()

    warm_repo_in_background()
    tasks = [asyncio.create_task(pipeline.checks_watcher())]
    try:
        yield
    finally:
        for task in [*tasks, _warmup]:
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title=f"{settings.brand_name} — заявки",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


# --- аутентификация -------------------------------------------------------

class Principal(BaseModel):
    login: str
    display_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def current_user(
    authorization: str | None = Header(None),
    session: str | None = Cookie(None, alias=SESSION_COOKIE),
    k: str | None = Query(None, description="токен из персональной ссылки"),
) -> Principal:
    # Пустой Authorization (браузер уже перешёл на cookie, а фронт по привычке
    # шлёт «Bearer ») не должен перекрывать cookie — поэтому не первый
    # непустой источник, а именно первый непустой ТОКЕН.
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[len("bearer ") :].strip()
    token = token or (session or "").strip() or (k or "").strip()

    found = auth.resolve(token)
    if not found:
        raise HTTPException(status_code=401, detail="Ссылка недействительна. Попросите новую у администратора.")
    login, profile = found
    return Principal(login=login, display_name=profile.get("display_name", login), role=profile.get("role", "user"))


def admin_only(user: Principal = Depends(current_user)) -> Principal:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user


def owned(request_id: int, user: Principal) -> dict[str, Any]:
    request = db.get_request(request_id)
    if not request or (request["user"] != user.login and not user.is_admin):
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return request


def forwarded_scheme(request: Request) -> str:
    """Схема с точки зрения браузера, а не контейнера.

    За TLS-терминатором (Dokploy, Traefik) в scope всегда http: uvicorn
    запущен без --proxy-headers, да и доверять им можно только заголовку
    прокси. Без этого персональная ссылка уезжала бы человеку по голому http
    вместе с живым токеном доступа.
    """
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return scheme or request.url.scheme


def base_url(request: Request) -> str:
    """Адрес инстанса без хвостового слэша — из него собираются ссылки доступа."""
    return str(request.base_url.replace(scheme=forwarded_scheme(request))).rstrip("/")


def request_or_404(request_id: int) -> dict[str, Any]:
    request = db.get_request(request_id)
    if not request:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return request


def claim(request: dict[str, Any], allowed: set[str], status: str, conflict: str, **fields: Any) -> None:
    """Занять заявку под операцию, переведя статус до запуска фоновой работы.

    Обработчики живут в одном цикле событий, поэтому проверка статуса и его
    смена без единого await между ними неделимы: второй клик по «Выкатить»
    (или двойная отправка ответа) увидит уже новый статус и получит 409,
    а не запустит вторую выкатку поверх первой.
    """
    if request["status"] not in allowed:
        raise HTTPException(status_code=409, detail=conflict)
    db.update_request(request["id"], status=status, **fields)
    updated = db.get_request(request["id"])
    if updated:
        bus.publish({"type": "request", "request": updated})


def enforce_rate_limit(user: Principal) -> None:
    """Один человек не должен занять всю очередь: заявки стоят денег и времени агента."""
    limit = settings.rate_limit_per_hour
    if limit <= 0 or user.is_admin:
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if db.count_requests_since(user.login, since) >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                "Слишком много заявок подряд. "
                f"Предел — {limit} в час, попробуйте ещё раз через несколько минут."
            ),
        )


# --- модели запросов ------------------------------------------------------

class NewRequest(BaseModel):
    body: str = Field(min_length=5, max_length=8000)
    images: list[str] = Field(default_factory=list, max_length=uploads.MAX_PER_MESSAGE)


class Answer(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    images: list[str] = Field(default_factory=list, max_length=uploads.MAX_PER_MESSAGE)


class NewPerson(BaseModel):
    login: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)


class DisabledFlag(BaseModel):
    disabled: bool


class PausedFlag(BaseModel):
    paused: bool


# --- API ------------------------------------------------------------------

@app.get("/api/me")
async def me(user: Principal = Depends(current_user)) -> dict[str, Any]:
    return {
        "login": user.login,
        "display_name": user.display_name,
        "role": user.role,
        "brand": {
            "name": settings.brand_name,
            "subtitle": settings.brand_subtitle,
            "accent": settings.brand_accent,
        },
        "project": {
            "site": settings.prod_url,
            "repo": settings.repo if user.is_admin else "",
        },
    }


@app.get("/api/meta")
async def meta(_: Principal = Depends(current_user)) -> dict[str, Any]:
    """Подписи статусов и лимиты приходят с сервера: фронт не должен знать их наизусть."""
    return {
        "statuses": pipeline.STATUS_META,
        "steps": pipeline.STEPS,
        "limits": {
            "max_images": uploads.MAX_PER_MESSAGE,
            "rate_limit_per_hour": settings.rate_limit_per_hour,
        },
        "approval_policy": settings.approval_policy,
        "paused": pipeline.paused(),
    }


@app.get("/api/requests")
async def list_requests(user: Principal = Depends(current_user)) -> dict[str, Any]:
    rows = db.list_requests(None if user.is_admin else user.login)
    for row in rows:
        row["author"] = auth.display_name(row["user"])
    return {"requests": rows}


@app.post("/api/requests", status_code=201)
async def create_request(payload: NewRequest, user: Principal = Depends(current_user)) -> dict[str, Any]:
    enforce_rate_limit(user)
    request_id = db.create_request(user.login, payload.body.strip())
    try:
        attached = uploads.attach(request_id, user.login, payload.images)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if attached:
        db.update_request(request_id, images=attached)
    await pipeline.emit(request_id, "system", "Заявка принята")
    if attached:
        await pipeline.emit(request_id, "system", f"Приложено картинок: {len(attached)}")
    pipeline.start(request_id)
    return db.get_request(request_id) or {"id": request_id}


@app.post("/api/uploads", status_code=201)
async def upload_image(
    file: UploadFile = File(...), user: Principal = Depends(current_user)
) -> dict[str, str]:
    """Картинка кладётся в личный черновик и прикрепляется при отправке заявки."""
    # Тот же часовой предел, что и на заявки: кто не может отправить заявку,
    # тому незачем и складывать под неё картинки в общий том.
    enforce_rate_limit(user)
    data = await file.read(uploads.MAX_BYTES + 1)
    try:
        name = uploads.save_staged(user.login, data)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": name}


@app.get("/api/uploads/{name}")
async def staged_image(name: str, user: Principal = Depends(current_user)) -> FileResponse:
    path = uploads.staged_path(user.login, name)
    if not path:
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    return FileResponse(
        str(path),
        media_type=uploads.content_type(name),
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
    )


@app.get("/api/requests/{request_id}/images/{name}")
async def request_image(
    request_id: int, name: str, user: Principal = Depends(current_user)
) -> FileResponse:
    request = owned(request_id, user)
    if name not in (request.get("images") or []):
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    path = uploads.attached_path(request_id, name)
    if not path:
        raise HTTPException(status_code=404, detail="Картинка не найдена")
    return FileResponse(
        str(path),
        media_type=uploads.content_type(name),
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/requests/{request_id}")
async def get_request(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    request["author"] = auth.display_name(request["user"])
    return {"request": request, "events": db.list_events(request_id)}


@app.post("/api/requests/{request_id}/answer")
async def answer(request_id: int, payload: Answer, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    try:
        attached = uploads.attach(request_id, user.login, payload.images)
    except uploads.UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if attached:
        db.update_request(request_id, images=[*(request.get("images") or []), *attached])
    # Статус уводим из needs_input сразу: пока агент разбирает ответ, второй
    # такой же ответ не должен запустить параллельный прогон в том же worktree.
    claim(request, {pipeline.NEEDS_INPUT}, pipeline.QUEUED, "Сейчас заявка не ждёт ответа")
    await pipeline.emit(request_id, "user", payload.text.strip())
    pipeline.start(request_id, followup=payload.text.strip(), images=attached)
    return {"ok": True}


@app.post("/api/requests/{request_id}/approve")
async def approve(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    if settings.approval_policy == "admin" and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="В этом проекте выкатку подтверждает администратор — правка уже ждёт его.",
        )
    claim(
        request,
        {pipeline.REVIEW},
        pipeline.MERGING,
        "Заявка ещё не готова к подтверждению",
        approved_by=user.login,
        approved_at=db.now(),
    )
    await pipeline.emit(request_id, "user", f"{user.display_name} подтвердил выкатку")
    pipeline.approve(request_id, approved_by=user.login)
    return {"ok": True}


# Отменить можно всё, что ещё не завершилось: словарь статусов пайплайна —
# единственный источник правды по их набору.
CANCELLABLE = set(pipeline.STATUS_META) - pipeline.FINAL


@app.post("/api/requests/{request_id}/cancel")
async def cancel(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    # Помечаем отменённой сразу, а PR и рабочую копию пайплайн уберёт следом:
    # для него cancelled — не «уже сделано», а «пора прибирать».
    claim(request, CANCELLABLE, pipeline.CANCELLED, "Заявка уже завершена")
    await pipeline.cancel(request_id)
    return {"ok": True}


@app.post("/api/requests/{request_id}/retry")
async def retry(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    if request["status"] in pipeline.ACTIVE:
        raise HTTPException(status_code=409, detail="Заявка уже в работе")
    if request.get("retried_at"):
        raise HTTPException(status_code=409, detail="Эту заявку уже отправили заново")
    enforce_rate_limit(user)
    # Отметка ставится до первого await: между проверкой и созданием новой
    # заявки есть паузы, а двойная отправка формы приходит в тот же процесс —
    # без синхронной отметки оба запроса завели бы по заявке и по прогону.
    db.update_request(request_id, retried_at=db.now())
    if request.get("pr_number") and request["status"] not in pipeline.FINAL:
        await pipeline.cancel(request_id)
    new_id = db.create_request(request["user"], request["body"])
    carried = uploads.clone(request_id, new_id, request.get("images") or [])
    if carried:
        db.update_request(new_id, images=carried)
    await pipeline.emit(new_id, "system", f"Повтор заявки №{request_id}")
    pipeline.start(new_id)
    return db.get_request(new_id) or {"id": new_id}


@app.get("/api/stream")
async def stream(request: Request, user: Principal = Depends(current_user)) -> StreamingResponse:
    queue = bus.subscribe()

    async def generator() -> AsyncIterator[str]:
        try:
            yield bus.sse({"type": "hello"})
            while True:
                # Шина отписывает медленного клиента сама. Не заметив этого,
                # мы бы вечно ждали событий в мёртвой очереди — поэтому
                # закрываем поток, а браузер переподключится и перечитает состояние.
                if not bus.is_subscribed(queue) or await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                owner = payload.get("user") or (payload.get("request") or {}).get("user")
                if owner and owner != user.login and not user.is_admin:
                    continue
                yield bus.sse(payload)
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- админка: чек-лист готовности ----------------------------------------

async def github_api(path: str, params: dict[str, str] | None = None) -> tuple[int, Any]:
    """Редкие ручки GitHub, нужные только чек-листу готовности.

    В рабочем цикле заявки они не участвуют, поэтому общему слою github.py
    не принадлежат: там живёт ровно то, без чего заявка не доедет до сайта.
    """
    key = config.github_token()
    if not key:
        raise github.GitHubError(
            "Ключ GitHub не задан — проверять нечем. "
            "Впишите его в админке, вкладка «Настройки»."
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{GITHUB_API}{path}", headers=headers, params=params)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"message": response.text[:300]}


def github_message(payload: Any) -> str:
    return str((payload or {}).get("message") or "") if isinstance(payload, dict) else ""


async def probe_github_token() -> tuple[bool, str]:
    data = await github.token_check()
    if not data.get("ok"):
        return False, str(data.get("error") or "GitHub не ответил")
    if not data.get("can_push"):
        return False, f"{data.get('repo')}: репозиторий виден, но записывать в него токен не может"
    # Пункт чек-листа требует ещё и прав на Pull requests, а в permissions
    # репозитория их не видно — поэтому спрашиваем отдельно и не красим
    # зелёным то, чего не проверяли.
    if data.get("pull_requests") is False:
        return False, f"{data.get('repo')}: писать в файлы можно, но к pull requests токен не пускают"
    return True, f"{data.get('repo')}: запись в файлы и pull requests доступны"


async def probe_branch_protection() -> tuple[bool, str]:
    status, payload = await github_api(
        f"/repos/{settings.repo}/branches/{settings.base_branch}/protection"
    )
    if status == 404:
        return False, f"ветка {settings.base_branch} ничем не защищена"
    if status >= 400:
        return False, f"GitHub {status}: {github_message(payload)}"
    contexts = ((payload or {}).get("required_status_checks") or {}).get("contexts") or []
    if contexts:
        return True, f"защита включена, обязательные проверки: {', '.join(contexts)}"
    return True, "защита включена, но обязательных проверок в ней нет"


async def probe_required_check() -> tuple[bool, str]:
    if not settings.required_check:
        return False, "проверка не задана — правка уйдёт на подтверждение без CI"
    status, payload = await github_api(f"/repos/{settings.repo}/actions/runs", {"per_page": "20"})
    if status >= 400:
        return False, f"GitHub {status}: {github_message(payload)}"
    runs = (payload or {}).get("workflow_runs") or []
    if not runs:
        return False, "в репозитории ещё не было ни одного прогона Actions"
    wanted = settings.required_check.lower()
    for run in runs:
        haystack = f"{run.get('name') or ''} {run.get('path') or ''}".lower()
        if wanted in haystack:
            return True, f"«{settings.required_check}» встречается в последних прогонах"
    names = ", ".join(sorted({str(r.get("name") or "?") for r in runs})[:5])
    return False, f"среди последних {len(runs)} прогонов «{settings.required_check}» нет (есть: {names})"


async def probe_agents_md() -> tuple[bool, str]:
    if not (settings.repo_dir / ".git").exists():
        return False, "локальной копии проекта ещё нет — проверять нечего"
    path = settings.repo_dir / "AGENTS.md"
    if not path.is_file():
        return False, "в корне проекта нет AGENTS.md"
    return True, f"AGENTS.md на месте, {path.stat().st_size} байт"


async def probe_run(argv: list[str]) -> tuple[int | None, str]:
    """Короткая диагностическая команда от имени агента: (код возврата, вывод).

    None вместо кода — команда не ответила вовремя. Своя группа процессов тут
    не роскошь: под sudo процесс чужой, прямой kill по нему не проходит, и без
    группового сигнала зависшая проверка подвесила бы всю вкладку «Готовность».
    """
    proc = await asyncio.create_subprocess_exec(
        *codex_runner.agent_command(argv),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=codex_runner.clean_env(),
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        await codex_runner.kill_group(proc)
        return None, ""
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


async def probe_codex_login() -> tuple[bool, str]:
    """Без авторизации Codex падает каждая заявка, а видно это только в логе агента."""
    code, text = await probe_run([settings.codex_bin, "login", "status"])
    if code is None:
        return False, "codex login status не ответил за отведённое время"
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if code == 0:
        return True, first or "авторизация есть"
    return False, first or f"codex завершился с кодом {code}"


async def probe_agent_isolation() -> tuple[bool, str]:
    """Работает ли агент отдельным пользователем — главный предохранитель шлюза.

    Верим не настройке, а прогону: правило sudoers могло не примениться, а
    обёртка — не доехать в образ. При совпадающем uid очистка окружения не
    защищает ничего: дочерний процесс читает /proc/<pid шлюза>/environ и
    забирает оттуда GitHub-токен и токены доступа обратно.
    """
    ready, reason = codex_runner.isolation()
    if not ready:
        return False, reason
    code, text = await probe_run(["id", "-u"])
    if code is None:
        return False, "обёртка запуска от имени агента не ответила"
    if code != 0 or not text.isdigit():
        return False, f"обёртка не отработала: {text[:200] or f'код возврата {code}'}"
    if int(text) == os.geteuid():
        return False, "прогон идёт тем же пользователем, что и шлюз"
    return True, f"{reason} (uid {text})"


async def probe_repo_clone() -> tuple[bool, str]:
    info = await gitops.repo_info()
    if not info.get("ok"):
        return False, str(info.get("error") or "копия проекта не готова")
    return True, f"{info.get('base')} на {info.get('head')} — {info.get('last_commit')}"


async def probe_deploy_tracking() -> tuple[bool, str]:
    mode = deploy.configured()
    if mode == "dokploy":
        return True, "статус сборки берём из Dokploy"
    if mode == "healthcheck":
        return True, f"следим за адресом {settings.health_url}"
    return False, "выкатка не отслеживается — заявка закроется сразу после мержа"


async def readiness_check(
    key: str, title: str, hint: str, probe: Callable[[], Awaitable[tuple[bool, str]]]
) -> dict[str, Any]:
    try:
        ok, detail = await probe()
    except github.GitHubError as exc:
        ok, detail = False, str(exc)
    except Exception as exc:  # noqa: BLE001 — красный пункт полезнее упавшей админки
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    return {"key": key, "title": title, "ok": ok, "detail": detail, "hint": hint}


@app.get("/api/admin/readiness")
async def admin_readiness(_: Principal = Depends(admin_only)) -> dict[str, Any]:
    """Всё, что должно быть настроено, чтобы первая же заявка доехала до сайта."""
    plan = [
        (
            "github_token",
            "Доступ к GitHub",
            "Fine-grained PAT на этот репозиторий: Contents — Read and write, Pull requests — Read and write. "
            "Вписывается на вкладке «Настройки» и действует сразу, без перезапуска.",
            probe_github_token,
        ),
        (
            "branch_protection",
            f"Защита ветки {settings.base_branch}",
            f"GitHub → Settings → Branches → правило для {settings.base_branch}. "
            "Без него сломанная проверка не помешает влить правку.",
            probe_branch_protection,
        ),
        (
            "required_check",
            f"Обязательная проверка «{settings.required_check}»" if settings.required_check
            else "Обязательная проверка в CI",
            "GITHUB_REQUIRED_CHECK должен совпадать с именем workflow или job в GitHub Actions.",
            probe_required_check,
        ),
        (
            "agents_md",
            "AGENTS.md в проекте",
            "Положите AGENTS.md в корень репозитория: агент читает его первым и узнаёт, чего трогать нельзя.",
            probe_agents_md,
        ),
        (
            "codex_login",
            "Codex авторизован",
            "Выполните в контейнере `codex login --device-auth` под пользователем агента или задайте CODEX_API_KEY.",
            probe_codex_login,
        ),
        (
            "agent_isolation",
            "Агент работает отдельным пользователем",
            "AGENT_USER, правило в /etc/sudoers.d и обёртка /usr/local/bin/run-agent.sh. "
            "Без них код проекта выполняется с секретами шлюза в окружении.",
            probe_agent_isolation,
        ),
        (
            "repo_clone",
            "Локальная копия проекта",
            "Копия появляется сама при старте. Если её нет — смотрите PROJECT_REPO и доступ к GitHub.",
            probe_repo_clone,
        ),
        (
            "deploy_tracking",
            "Отслеживание выкатки",
            "Заполните DOKPLOY_URL/TOKEN/APPLICATION_ID или хотя бы PROJECT_PROD_URL с health-адресом.",
            probe_deploy_tracking,
        ),
    ]
    checks = await asyncio.gather(*(readiness_check(*item) for item in plan))
    # Опечатка в переменной окружения не ловится ни одной проверкой выше:
    # там смотрят на внешний мир, а тут — на то, с чем запустился сам шлюз.
    return {"checks": list(checks), "problems": settings.problems()}


# --- админка: ключ GitHub -------------------------------------------------

# Ключ в таблице настроек тот же, что читает config.github_token(), поэтому
# сохранённое значение действует со следующего запроса: ни перезапуска, ни
# редеплоя не нужно — ради этого форма и делалась.
GITHUB_TOKEN_KEY = "github_token"
GITHUB_TOKEN_CHECKED_KEY = "github_token_checked_at"

# Fine-grained PAT — это github_pat_… под сотню символов, classic — сорок.
# Границы намеренно широкие: дело проверки формы — отсечь «вставил не то
# поле», а годность ключа всё равно решает сам GitHub.
TOKEN_MIN_LEN = 20
TOKEN_MAX_LEN = 255


def token_hint(token: str) -> str:
    """«…f3a2» — ровно столько, чтобы отличить один ключ от другого.

    Подсказка уходит и в интерфейс, и в лог, поэтому больше четырёх символов
    не отдаём нигде и никогда: остаток токена — это уже часть секрета.
    """
    if not token:
        return ""
    if len(token) < 8:
        # Такой короткой строки у настоящего ключа не бывает, но если она
        # сюда попала, хвост из четырёх символов — это половина значения.
        return "…"
    return f"…{token[-4:]}"


def github_token_state() -> dict[str, Any]:
    """Что шлюз знает о ключе GitHub, без самого ключа.

    Живьём в GitHub отсюда не ходим: вкладку «Настройки» открывают часто, а
    правду о токене в реальном времени показывает чек-лист готовности.
    can_push и checked_at — это память о боевой проверке, которую ключ прошёл
    в момент сохранения, а не состояние прямо сейчас.
    """
    stored = db.get_setting(GITHUB_TOKEN_KEY)
    effective = config.github_token()
    # Значение из интерфейса важнее env, поэтому непустой stored — это всегда
    # «ui», а непустой действующий ключ без stored мог прийти только из env.
    source = "ui" if stored else ("env" if effective else "none")
    checked_at = db.get_setting(GITHUB_TOKEN_CHECKED_KEY) if source == "ui" else ""
    # Без PROJECT_REPO проверять ключ не на чем, и молчать об этом нельзя:
    # администратор будет вставлять токен за токеном и не поймёт, почему
    # «Доступ к GitHub» остаётся красным.
    error = None if settings.repo else "PROJECT_REPO не задан — проверять ключ не на чем"
    return {
        "configured": source != "none",
        "source": source,
        "hint": token_hint(effective),
        "repo": settings.repo,
        # Сохраняем ключ только после успешной проверки на запись, поэтому у
        # «ui» право писать было; про ключ из env этой ручке ничего не известно.
        "can_push": True if source == "ui" else None,
        "checked_at": checked_at or None,
        "error": error,
    }


def validated_token(raw: str) -> str:
    """Форма ключа. Человеческий отказ здесь дешевле похода в GitHub."""
    # Из буфера обмена регулярно приезжают перенос строки и пробел по краям —
    # это не повод отказывать. А вот пробел внутри значит, что скопировали не
    # ключ, а строку вокруг него.
    token = raw.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Поле пустое — вставьте ключ.")
    if any(char.isspace() for char in token):
        raise HTTPException(
            status_code=400,
            detail="В ключе есть пробел или перенос строки — похоже, скопировалось лишнее.",
        )
    if not token.isascii() or not token.isprintable():
        # Кириллица и управляющие символы не переживут заголовок Authorization:
        # запрос упадёт где-то в глубине httpx, и причину придётся угадывать.
        raise HTTPException(
            status_code=400,
            detail="В ключе есть посторонние символы — в токене GitHub только латиница и цифры.",
        )
    if not TOKEN_MIN_LEN <= len(token) <= TOKEN_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ключ должен быть длиной от {TOKEN_MIN_LEN} до {TOKEN_MAX_LEN} символов, "
                f"а в этом — {len(token)}."
            ),
        )
    return token


async def check_token_can_push(token: str) -> None:
    """Боевая проверка ключа: видит ли он репозиторий и может ли в него писать.

    Спрашиваем GitHub до сохранения. Подменить рабочий ключ на нерабочий и
    узнать об этом на первой же заявке — худшее, что может случиться с этой
    формой, поэтому непроверенное значение в базу не попадает.
    """
    try:
        result = await github.token_check(token)
    except httpx.HTTPError as exc:
        # Наружу — ни текста исключения, ни адреса: они ходили рядом с ключом,
        # а администратору хватит и того, что связаться не удалось.
        logger.warning("Проверка ключа GitHub не состоялась: %s", exc.__class__.__name__)
        raise HTTPException(
            status_code=502,
            detail="Не удалось связаться с GitHub, чтобы проверить ключ. Попробуйте ещё раз.",
        ) from exc

    if not result.get("ok"):
        # Ответ GitHub дописываем к своей формулировке: за отказом стоит не
        # только «не тот PAT», но и запрет организации (SSO, политика PAT), а
        # его без текста GitHub администратор будет искать не там. Ключ из
        # чужого текста вырезаем, даже если GitHub его никогда не возвращает.
        answer = str(result.get("error") or "GitHub не объяснил отказ").replace(token, "***")
        raise HTTPException(
            status_code=400,
            detail=(
                f"Этот токен не видит репозиторий {settings.repo}. "
                f"Проверьте, что PAT выдан на него. ({answer})"
            ),
        )
    if not result.get("can_push"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Токен видит репозиторий, но не может писать. Нужны права "
                "Contents: Read and write и Pull requests: Read and write."
            ),
        )
    # Права на Pull requests — отдельный переключатель PAT, и в правах
    # репозитория его не видно. Без этой проверки ключ сохранялся зелёным, а
    # заявка падала на открытии PR: прогон агента уже оплачен, ветка ушла, а
    # сотрудник видит сырое «Resource not accessible by personal access token».
    if result.get("pull_requests") is False:
        raise HTTPException(
            status_code=400,
            detail=(
                "Токен видит репозиторий и может писать в файлы, но не имеет прав "
                "Pull requests: Read and write — открыть pull request он не сможет. "
                "Добавьте это право в настройках PAT."
            ),
        )


class GithubTokenIn(BaseModel):
    # Ограничений через Field здесь нет намеренно: на их нарушение FastAPI
    # отвечает 422, а в теле такого ответа лежит отвергнутое значение — то
    # есть сам ключ. Форму проверяет validated_token и отвечает текстом.
    token: str


@app.get("/api/admin/github-token")
async def admin_github_token(_: Principal = Depends(admin_only)) -> dict[str, Any]:
    """Состояние доступа к GitHub. Самого ключа в ответе нет — только хвост."""
    return github_token_state()


@app.put("/api/admin/github-token")
async def admin_save_github_token(
    payload: GithubTokenIn, user: Principal = Depends(admin_only)
) -> dict[str, Any]:
    """Сохранить ключ — но только после того, как GitHub подтвердит его боем."""
    token = validated_token(payload.token)
    if not settings.repo:
        raise HTTPException(
            status_code=400,
            detail=(
                "PROJECT_REPO не задан, и проверять ключ не на чем. "
                "Сначала укажите репозиторий в переменных окружения инстанса."
            ),
        )
    await check_token_can_push(token)
    db.set_setting(GITHUB_TOKEN_KEY, token)
    db.set_setting(
        GITHUB_TOKEN_CHECKED_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    # В логе — только хвост и длина: журнал шлюза читают и в поддержке, и в
    # чужих глазах он не должен превращаться в готовый доступ к репозиторию.
    logger.info(
        "%s заменил ключ GitHub через интерфейс: %s, длина %d",
        user.login,
        token_hint(token),
        len(token),
    )
    # Ключ обычно вписывают на инстансе, где стартовый клон приватного
    # репозитория уже провалился. Пробуем ещё раз прямо сейчас, чтобы человек
    # увидел зелёный чек-лист, а не остался с красной «Локальной копией».
    warm_repo_in_background()
    return github_token_state()


@app.delete("/api/admin/github-token")
async def admin_forget_github_token(user: Principal = Depends(admin_only)) -> dict[str, Any]:
    """Забыть ключ из интерфейса и вернуться к переменной окружения.

    Пишем пустое значение вместо удаления строки: config.github_token()
    считает пустое отсутствующим и сам падает обратно на env.
    """
    db.set_setting(GITHUB_TOKEN_KEY, "")
    db.set_setting(GITHUB_TOKEN_CHECKED_KEY, "")
    logger.info("%s удалил ключ GitHub, заданный через интерфейс", user.login)
    return github_token_state()


# --- админка: расход и журнал --------------------------------------------

@app.get("/api/admin/usage")
async def admin_usage(
    days: int = Query(30, ge=1, le=365), _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    totals = db.usage_totals(days)
    for row in totals:
        row["author"] = auth.display_name(row.get("user", ""))
    return {"totals": totals, "days": days}


@app.get("/api/admin/journal")
async def admin_journal(
    limit: int = Query(50, ge=1, le=500), _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    entries = db.approvals_journal(limit)
    for row in entries:
        row["author"] = auth.display_name(row.get("user", ""))
        row["approver"] = auth.display_name(row["approved_by"]) if row.get("approved_by") else ""
    return {"entries": entries}


# --- админка: люди --------------------------------------------------------

def known_person(login: str) -> dict[str, Any]:
    person = db.get_person(login)
    if not person:
        raise HTTPException(status_code=404, detail="Человек не найден")
    return person


@app.get("/api/admin/people")
async def admin_people(request: Request, _: Principal = Depends(admin_only)) -> dict[str, Any]:
    return {"people": auth.access_links(base_url(request))}


@app.post("/api/admin/people", status_code=201)
async def admin_add_person(
    payload: NewPerson, request: Request, _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    try:
        person = auth.add_person(payload.login, payload.display_name)
    except auth.PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Логин берём из ответа, а не из запроса: add_person приводит его к нижнему
    # регистру, и «Anna» из формы завелась бы как «anna» — искать её обратно
    # по исходной строке значит не найти только что созданного человека.
    return auth.access_link(base_url(request), person["login"])


@app.post("/api/admin/people/{login}/disable")
async def admin_disable_person(
    login: str, payload: DisabledFlag, request: Request, _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    known_person(login)
    try:
        if payload.disabled:
            auth.disable_person(login)
        else:
            auth.enable_person(login)
    except auth.PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return auth.access_link(base_url(request), login)


@app.post("/api/admin/people/{login}/rotate")
async def admin_rotate_person(
    login: str, request: Request, _: Principal = Depends(admin_only)
) -> dict[str, str]:
    """Старая ссылка перестаёт работать сразу — этим и забирают доступ."""
    known_person(login)
    try:
        token = auth.rotate_token(login)
    except auth.PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"link": f"{base_url(request)}/?k={token}"}


# --- админка: приём заявок и разбор конкретной заявки ---------------------

@app.post("/api/admin/pause")
async def admin_pause(payload: PausedFlag, _: Principal = Depends(admin_only)) -> dict[str, Any]:
    db.set_setting("intake_paused", "1" if payload.paused else "0")
    if not payload.paused:
        # Пока стояла пауза, заявки копились в очереди — теперь их пора разобрать.
        pipeline.resume_queued()
    return {"paused": pipeline.paused()}


def attempt_number(path: Path) -> int:
    """req-12-3.jsonl → 3: попытки нумеруются по порядку, нужен последний прогон."""
    suffix = path.stem.rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def read_log_tail(request_id: int, tail: int) -> list[str]:
    """Хвост последнего прогона агента. Файл читаем потоком: он бывает в десятки мегабайт."""
    logs = sorted(settings.logs_dir.glob(f"req-{request_id}-*.jsonl"), key=attempt_number)
    if not logs:
        return []
    with logs[-1].open("r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in deque(handle, maxlen=tail)]


@app.get("/api/admin/requests/{request_id}/log")
async def admin_request_log(
    request_id: int, tail: int = Query(200, ge=1, le=5000), _: Principal = Depends(admin_only)
) -> dict[str, Any]:
    request_or_404(request_id)
    return {"lines": await asyncio.to_thread(read_log_tail, request_id, tail)}


@app.get("/api/admin/requests/{request_id}/tests")
async def admin_request_tests(request_id: int, _: Principal = Depends(admin_only)) -> dict[str, str]:
    return {"output": request_or_404(request_id).get("tests_output") or ""}


@app.post("/api/admin/requests/{request_id}/force-cancel")
async def admin_force_cancel(request_id: int, _: Principal = Depends(admin_only)) -> dict[str, Any]:
    request_or_404(request_id)
    await pipeline.force_cancel(request_id)
    return {"ok": True}


@app.post("/api/admin/requests/{request_id}/recheck")
async def admin_recheck(request_id: int, _: Principal = Depends(admin_only)) -> dict[str, Any]:
    """Опрос проверок GitHub вне очереди: сторож ходит раз в 20 секунд, а ждать не хочется."""
    request = request_or_404(request_id)
    if not request.get("pr_number"):
        raise HTTPException(status_code=409, detail="У заявки ещё нет PR — проверять нечего")
    if request["status"] not in pipeline.RECHECKABLE:
        # Иначе кнопка воскрешала бы завершённую заявку: refresh_checks увёл бы
        # её обратно в review, человек увидел бы «Выкатить» на уже смерженном
        # PR, а GitHub ответил бы на это 405.
        raise HTTPException(
            status_code=409, detail="Заявка уже прошла этот этап — проверять нечего"
        )
    # Переходы статусов по результату проверки живут в пайплайне — дублировать
    # их здесь нельзя, иначе сторож и кнопка начнут расходиться.
    await pipeline.refresh_checks(request)
    return {"ok": True}


@app.get("/api/health-check")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# --- статика --------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index(request: Request, k: str | None = Query(None)) -> Response:
    """Персональная ссылка ?k=… обменивается на cookie и тут же исчезает из адреса.

    Иначе токен остаётся в истории браузера, в закладках и в Referer — а это
    та самая ссылка, по которой любой войдёт под этим человеком.
    """
    if k and auth.resolve(k):
        response: Response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            k,
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=forwarded_scheme(request) == "https",
            path="/",
        )
        return response
    return FileResponse(str(STATIC_DIR / "index.html"))
