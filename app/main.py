"""HTTP-слой: заявки, лента событий, админка."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, bus, db, deploy, github, gitops, pipeline
from .config import settings

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    db.connect()
    await pipeline.recover_after_restart()
    application.state.repo_ready = False
    application.state.repo_error = ""

    async def warm_repo() -> None:
        try:
            await gitops.ensure_repo()
            application.state.repo_ready = True
        except Exception as exc:  # noqa: BLE001 — покажем в админке, но сервис не уроним
            application.state.repo_error = str(exc)

    tasks = [asyncio.create_task(pipeline.checks_watcher()), asyncio.create_task(warm_repo())]
    try:
        yield
    finally:
        for task in tasks:
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
    k: str | None = Query(None, description="токен из персональной ссылки"),
) -> Principal:
    token = k
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    found = auth.resolve(token)
    if not found:
        raise HTTPException(status_code=401, detail="Ссылка недействительна. Попросите новую у администратора.")
    login, profile = found
    return Principal(login=login, display_name=profile.get("display_name", login), role=profile.get("role", "user"))


def owned(request_id: int, user: Principal) -> dict[str, Any]:
    request = db.get_request(request_id)
    if not request or (request["user"] != user.login and not user.is_admin):
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return request


# --- модели запросов ------------------------------------------------------

class NewRequest(BaseModel):
    body: str = Field(min_length=5, max_length=8000)


class Answer(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


# --- жизненный цикл приложения -------------------------------------------

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


@app.get("/api/requests")
async def list_requests(user: Principal = Depends(current_user)) -> dict[str, Any]:
    rows = db.list_requests(None if user.is_admin else user.login)
    for row in rows:
        row["author"] = auth.display_name(row["user"])
    return {"requests": rows}


@app.post("/api/requests", status_code=201)
async def create_request(payload: NewRequest, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request_id = db.create_request(user.login, payload.body.strip())
    await pipeline.emit(request_id, "system", "Заявка принята")
    pipeline.start(request_id)
    return db.get_request(request_id) or {"id": request_id}


@app.get("/api/requests/{request_id}")
async def get_request(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    request["author"] = auth.display_name(request["user"])
    return {"request": request, "events": db.list_events(request_id)}


@app.post("/api/requests/{request_id}/answer")
async def answer(request_id: int, payload: Answer, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    if request["status"] != pipeline.NEEDS_INPUT:
        raise HTTPException(status_code=409, detail="Сейчас заявка не ждёт ответа")
    await pipeline.emit(request_id, "user", payload.text.strip())
    pipeline.start(request_id, followup=payload.text.strip())
    return {"ok": True}


@app.post("/api/requests/{request_id}/approve")
async def approve(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    if request["status"] != pipeline.REVIEW:
        raise HTTPException(status_code=409, detail="Заявка ещё не готова к подтверждению")
    await pipeline.emit(request_id, "user", f"{user.display_name} подтвердил выкатку")
    pipeline.approve(request_id)
    return {"ok": True}


@app.post("/api/requests/{request_id}/cancel")
async def cancel(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    owned(request_id, user)
    await pipeline.cancel(request_id)
    return {"ok": True}


@app.post("/api/requests/{request_id}/retry")
async def retry(request_id: int, user: Principal = Depends(current_user)) -> dict[str, Any]:
    request = owned(request_id, user)
    if request["status"] in pipeline.ACTIVE:
        raise HTTPException(status_code=409, detail="Заявка уже в работе")
    if request.get("pr_number") and request["status"] not in pipeline.FINAL:
        await pipeline.cancel(request_id)
    new_id = db.create_request(request["user"], request["body"])
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
                if await request.is_disconnected():
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


# --- админка --------------------------------------------------------------

@app.get("/api/admin/overview")
async def admin_overview(request: Request, user: Principal = Depends(current_user)) -> dict[str, Any]:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")
    base = str(request.base_url).rstrip("/")

    async def probe(coro: Any) -> dict[str, Any]:
        """Одна упавшая проверка не должна ронять всю админку."""
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

    return {
        "config_problems": settings.problems(),
        "repo": await probe(gitops.repo_info()),
        "github": await probe(github.token_check()),
        "deploy_mode": deploy.configured(),
        "sandbox": {
            "mode": settings.codex_sandbox,
            "network": settings.codex_network,
            "model": settings.codex_model or "по умолчанию",
        },
        "access_links": auth.access_links(base),
        "runtime": {
            "repo_ready": getattr(app.state, "repo_ready", False),
            "repo_error": getattr(app.state, "repo_error", ""),
            "max_concurrent": settings.max_concurrent,
        },
    }


@app.get("/api/health-check")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# --- статика --------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))
