"""Наполняет тестовую базу заявками во всех состояниях — чтобы посмотреть интерфейс.

    DATA_DIR=/tmp/gw-ui .venv/bin/python tests/seed_ui.py
    DATA_DIR=/tmp/gw-ui .venv/bin/uvicorn app.main:app --port 8799
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DATA_DIR", "/tmp/gw-ui")
os.environ.setdefault("USERS", "anna:Анна Петрова,oleg:Олег Смирнов")
os.environ.setdefault("ADMIN_TOKEN", "demo-admin-token")
os.environ.setdefault("PROJECT_REPO", "acme/cvetbiz")
os.environ.setdefault("PROJECT_PROD_URL", "https://example.com")
os.environ.setdefault("BRAND_NAME", "Правки")
os.environ.setdefault("BRAND_SUBTITLE", "cvetbiz.digital")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, db  # noqa: E402

SAMPLES = [
    dict(
        user="anna",
        body="На странице «Доставка» поменяйте телефон на +7 999 123-45-67, старый уже не работает.",
        status="review",
        title="Новый телефон на странице доставки",
        summary="Заменил телефон на странице «Доставка» и в подвале сайта — теперь везде новый номер.",
        user_visible=["На странице «Доставка» новый номер", "В подвале сайта тот же номер"],
        files=["M templates/delivery.html", "M templates/footer.html"],
        text_changes=[
            {"file": "templates/delivery.html", "before": "Телефон: +7 000 000-00-00", "after": "Телефон: +7 999 123-45-67"},
            {"file": "templates/footer.html", "before": "Звоните: +7 000 000-00-00", "after": "Звоните: +7 999 123-45-67"},
        ],
        checks_status="success",
        pr_number=128,
        pr_url="https://github.com/acme/cvetbiz/pull/128",
        risk="low",
        events=[
            ("system", "Заявка принята"),
            ("progress", "Изучаю проект"),
            ("agent", "Нашёл телефон в двух местах: на странице доставки и в подвале. Меняю оба."),
            ("progress", "Правлю: delivery.html, footer.html"),
            ("progress", "Сохраняю изменения"),
            ("system", "Автоматическая проверка прошла. Ждём вашего подтверждения"),
        ],
    ),
    dict(
        user="anna",
        body="Добавьте на главную блок с отзывами клиентов",
        status="needs_input",
        title="Блок отзывов на главной",
        question="Откуда брать отзывы — забирать из уже существующего раздела «Отзывы» или вы дадите текст руками?",
        events=[
            ("system", "Заявка принята"),
            ("progress", "Изучаю проект"),
            ("agent", "Откуда брать отзывы — забирать из уже существующего раздела «Отзывы» или вы дадите текст руками?"),
        ],
    ),
    dict(
        user="anna",
        body="Сделайте кнопку «Заказать» заметнее — сейчас её не видно на телефоне",
        status="checking",
        title="Кнопка «Заказать» на мобильном",
        summary="Сделал кнопку крупнее и закрепил её внизу экрана на телефонах.",
        pr_number=129,
        pr_url="https://github.com/acme/cvetbiz/pull/129",
        events=[
            ("system", "Взял заявку в работу"),
            ("progress", "Изучаю проект"),
            ("progress", "Правлю: main.css"),
        ],
    ),
    dict(
        user="anna",
        body="Уберите старую акцию «Весна-2025» со всех страниц",
        status="done",
        title="Снял акцию «Весна-2025»",
        summary="Убрал баннер акции с главной, со страницы каталога и из писем.",
        user_visible=["Баннера акции больше нет ни на одной странице"],
        files=["M templates/index.html", "M templates/catalog.html", "D templates/promo_spring.html"],
        merged_at=db.now(),
        deployed_at=db.now(),
        events=[("system", "Заявка принята"), ("system", "Готово — правка на сайте")],
    ),
    dict(
        user="anna",
        body="Поменяйте цены в прайсе: розы 1500, тюльпаны 900",
        status="tests_failed",
        title="Новые цены на розы и тюльпаны",
        error="Автоматическая проверка не прошла",
        checks_detail="проверка завершилась статусом failure",
        events=[
            ("system", "Заявка принята"),
            ("progress", "Правлю: prices.json"),
            ("error", "Автоматическая проверка не прошла"),
        ],
    ),
]


def main() -> None:
    db.connect()
    for sample in SAMPLES:
        events = sample.pop("events", [])
        user = sample.pop("user")
        body = sample.pop("body")
        request_id = db.create_request(user, body)
        db.update_request(request_id, **sample)
        for kind, text in events:
            db.add_event(request_id, kind, text)
    print(f"База: {os.environ['DATA_DIR']}")
    for person in auth.access_links("http://127.0.0.1:8799"):
        print(f"  {person['display_name']:<16} {person['link']}")


if __name__ == "__main__":
    main()
