"""Мини-шина событий для live-обновлений интерфейса (SSE).

Держим в памяти: если браузер отвалился и переподключился, он перечитывает
состояние заявки обычным GET, а лента событий лежит в базе.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _subscribers.discard(queue)


def publish(payload: dict[str, Any]) -> None:
    """Разослать событие всем открытым вкладкам. Никогда не блокирует."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Медленный клиент: пусть переспросит состояние обычным запросом.
            unsubscribe(queue)


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
