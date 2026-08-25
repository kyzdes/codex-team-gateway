"""Отслеживание выкатки после мержа.

Сам шлюз ничего не деплоит: после мержа в основную ветку срабатывает
вебхук Dokploy, он собирает образ и перезапускает приложение. Наша задача —
честно показать человеку, доехала ли его правка до сайта.

Источник правды по возможности — Dokploy API; если он не настроен,
опираемся на health-адрес продакшена.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import httpx

from .config import settings

Progress = Callable[[str, str], Awaitable[None]]

DONE_STATES = {"done", "success", "completed"}
FAIL_STATES = {"error", "failed", "cancelled"}


def configured() -> str:
    if settings.dokploy_url and settings.dokploy_token and settings.dokploy_application_id:
        return "dokploy"
    if settings.health_url:
        return "healthcheck"
    return "none"


async def _dokploy_deployments() -> list[dict[str, Any]]:
    url = f"{settings.dokploy_url}/api/deployment.all"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            url,
            params={"applicationId": settings.dokploy_application_id},
            headers={"x-api-key": settings.dokploy_token, "accept": "application/json"},
        )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else payload.get("deployments", [])


async def _healthcheck_ok() -> bool:
    if not settings.health_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(settings.health_url)
        return response.status_code < 400
    except httpx.HTTPError:
        return False


async def wait_for_deploy(on_progress: Progress) -> tuple[bool, str]:
    mode = configured()
    deadline = time.monotonic() + settings.deploy_timeout

    if mode == "dokploy":
        await on_progress("progress", "Собираю новую версию сайта")
        seen_running = False
        while time.monotonic() < deadline:
            try:
                deployments = await _dokploy_deployments()
            except (httpx.HTTPError, ValueError) as exc:
                return await _fallback(on_progress, f"Dokploy не ответил ({exc.__class__.__name__})")
            latest = deployments[0] if deployments else None
            status = str((latest or {}).get("status", "")).lower()
            if status in DONE_STATES and seen_running:
                return True, "Выкачено"
            if status in FAIL_STATES:
                return False, "Сборка не прошла — правка осталась только в коде"
            if status in {"running", "in_progress"}:
                seen_running = True
            await asyncio.sleep(8)
        return False, "Выкатка идёт дольше обычного — загляните в Dokploy"

    if mode == "healthcheck":
        return await _wait_for_health(on_progress, deadline)

    await on_progress("progress", "Изменения приняты")
    return True, "Изменения в основной ветке. Выкатка отслеживается вручную."


async def _fallback(on_progress: Progress, reason: str) -> tuple[bool, str]:
    await on_progress("progress", f"{reason}, проверяю сайт напрямую")
    return await _wait_for_health(on_progress, time.monotonic() + settings.deploy_timeout)


async def _wait_for_health(on_progress: Progress, deadline: float) -> tuple[bool, str]:
    if not settings.health_url:
        return True, "Изменения в основной ветке"
    await on_progress("progress", "Жду, пока сайт перезапустится с правкой")
    # Сначала даём сервису время уйти на перезапуск, затем ждём стабильных ответов.
    await asyncio.sleep(20)
    stable = 0
    while time.monotonic() < deadline:
        stable = stable + 1 if await _healthcheck_ok() else 0
        if stable >= 3:
            return True, "Сайт снова доступен"
        await asyncio.sleep(6)
    return False, "Сайт не ответил за отведённое время — стоит проверить вручную"
