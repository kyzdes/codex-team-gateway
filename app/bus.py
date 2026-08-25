"""Мини-шина событий для live-обновлений интерфейса (SSE).

Держим в памяти: если браузер отвалился и переподключился, он перечитывает
состояние заявки обычным GET, а лента событий лежит в базе.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

Event = dict[str, Any]

# Потолок на вкладку. Столько событий подряд заявка не выдаёт даже на пике,
# так что упереться в него может только клиент, который перестал читать.
QUEUE_LIMIT = 256

_subscribers: set[asyncio.Queue[Event]] = set()


def subscribe() -> asyncio.Queue[Event]:
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_LIMIT)
    _subscribers.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue[Event]) -> None:
    _subscribers.discard(queue)


def is_subscribed(queue: asyncio.Queue[Event]) -> bool:
    """Жива ли подписка.

    Раньше отписка была односторонней: publish() выкидывал отставшего клиента,
    а генератор в main.py об этом не узнавал и продолжал ждать на queue.get().
    Лента у человека молча замирала до перезагрузки страницы — самый неприятный
    вид поломки, потому что снаружи всё выглядит работающим.

    Поэтому генератор обязан спрашивать это после каждого ожидания и, получив
    False, завершать ответ: браузер переподключится сам и перечитает состояние.
    """
    return queue in _subscribers


def publish(payload: Event) -> None:
    """Разослать событие всем открытым вкладкам. Никогда не блокирует."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Медленный клиент: пусть переспросит состояние обычным запросом.
            unsubscribe(queue)
            _drain(queue)
            log.warning("SSE-клиент не успевает читать ленту, отписываем")


def _drain(queue: asyncio.Queue[Event]) -> None:
    """Выбросить накопленное у отписанного клиента.

    Досылать эти события смысла нет — они уже устарели, а человек всё равно
    перечитает состояние после переподключения. Зато пустая очередь позволяет
    генератору дойти до проверки is_subscribed() и выйти сразу, а не после
    того, как он выплюнет в браузер весь застоявшийся хвост.
    """
    # Между empty() и get_nowait() нет await, так что перехватить у нас
    # событие никто не успеет — цикл безопасен.
    while not queue.empty():
        queue.get_nowait()


def sse(payload: Event) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
