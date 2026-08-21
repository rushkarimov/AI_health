"""Самодиагностика для кнопки «Поддержка».

Отвечает на вопрос «всё ли живо»: база, внешние API, свежесть выгрузки из
Apple Health, объём данных, расписание. Проверки идут параллельно и каждая
со своим таймаутом — иначе одна залипшая внешняя ручка задержала бы весь отчёт.

Ничего не пишет: только читает и опрашивает.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from typing import Any

import httpx

from app.config import get_settings
from app.db import repo

log = logging.getLogger(__name__)

CHECK_TIMEOUT = 8.0

OK = "🟢"
WARN = "🟡"
FAIL = "🔴"


async def _timed(coro) -> tuple[Any, float]:
    started = time.monotonic()
    result = await coro
    return result, (time.monotonic() - started) * 1000


# ------------------------------------------------------------------ проверки

async def check_db() -> dict[str, Any]:
    try:
        (rows, ms) = await _timed(repo.fetch("SELECT 1 AS ok"))
        if rows and rows[0].get("ok") == 1:
            return {"mark": OK, "text": f"Postgres — отвечает ({ms:.0f} мс)"}
        return {"mark": FAIL, "text": "Postgres — неожиданный ответ"}
    except Exception as e:
        return {"mark": FAIL, "text": f"Postgres — недоступен: {type(e).__name__}"}


async def _routerai_ping(model: str, label: str) -> dict[str, Any]:
    """Один короткий запрос к модели через RouterAI.

    Дешёвый способ проверить, что и ключ жив, и модель доступна: 5 токенов
    на ответ обходятся в сотые доли копейки.
    """
    s = get_settings()
    if not s.routerai_api_key:
        return {"mark": FAIL, "text": f"{label} — нет ключа RouterAI"}

    from openai import AsyncOpenAI

    from app.providers.routerai import BASE_URL

    client = AsyncOpenAI(api_key=s.routerai_api_key, base_url=BASE_URL, timeout=20.0)
    try:
        r, ms = await _timed(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=5,
            )
        )
        if r.choices:
            return {"mark": OK, "text": f"{label} — отвечает ({ms:.0f} мс)"}
        return {"mark": WARN, "text": f"{label} — пустой ответ"}
    except Exception as e:
        text = str(e)[:60]
        if "money" in text.lower() or "402" in text:
            return {"mark": FAIL, "text": f"{label} — нет средств на балансе"}
        if "401" in text or "403" in text:
            return {"mark": FAIL, "text": f"{label} — ключ отклонён"}
        return {"mark": FAIL, "text": f"{label} — {type(e).__name__}"}


async def check_llm() -> dict[str, Any]:
    s = get_settings()
    if s.llm_provider != "routerai":
        return {"mark": WARN, "text": f"Текст — провайдер {s.llm_provider}, не проверяю"}
    return await _routerai_ping(s.routerai_llm_model, "Текст и вопросы")


async def check_vision() -> dict[str, Any]:
    """Модель для фото. Картинку не отправляем — проверяем доступность модели:
    полноценный запрос с изображением стоил бы 0.02 ₽ на каждое нажатие."""
    s = get_settings()
    if s.vision_provider != "routerai":
        return {"mark": WARN, "text": f"Фото — провайдер {s.vision_provider}, не проверяю"}
    return await _routerai_ping(s.routerai_vision_model, "Распознавание фото")


async def check_stt() -> dict[str, Any]:
    """Голос: у транскрипции нет дешёвого health-check, поэтому проверяем,
    что модель есть в списке доступных на RouterAI."""
    s = get_settings()
    if s.stt_provider != "routerai":
        return {"mark": WARN, "text": f"Голос — провайдер {s.stt_provider}, не проверяю"}
    if not s.routerai_api_key:
        return {"mark": FAIL, "text": "Голос — нет ключа RouterAI"}

    from openai import AsyncOpenAI

    from app.providers.routerai import BASE_URL

    client = AsyncOpenAI(api_key=s.routerai_api_key, base_url=BASE_URL, timeout=20.0)
    try:
        r, ms = await _timed(client.models.list())
        names = {m.id for m in r.data}
        if s.routerai_stt_model in names:
            return {"mark": OK, "text": f"Голос в текст — доступен ({ms:.0f} мс)"}
        return {"mark": WARN, "text": f"Голос — модели {s.routerai_stt_model} нет в списке"}
    except Exception as e:
        return {"mark": FAIL, "text": f"Голос — {type(e).__name__}"}


async def check_profile(user_id: int) -> dict[str, Any]:
    """Заполнен ли профиль.

    Появился вместе с регистрацией через мини-апп: без роста и пола не
    считаются ни ИМТ (колонка есть с миграции 007), ни норма калорий, и раздел
    «Рекомендации» опирается на неполные данные, молча об этом не сообщая.
    """
    try:
        user = await repo.get_user(user_id)
        if not user:
            return {"mark": WARN, "text": "Профиль — не заполнен (нет в таблице users)"}

        missing = [
            label for key, label in (
                ("height_cm", "рост"), ("sex", "пол"), ("birth_date", "дата рождения"),
            ) if not user.get(key)
        ]
        if missing:
            return {"mark": WARN,
                    "text": f"Профиль — не хватает: {', '.join(missing)}"}

        return {
            "mark": OK,
            "text": f"Профиль — {user['name']}, {user.get('age', '?')} лет, "
                    f"{float(user['height_cm']):.0f} см",
        }
    except Exception as e:
        return {"mark": FAIL, "text": f"Профиль — ошибка запроса: {type(e).__name__}"}


async def check_health_export(user_id: int) -> dict[str, Any]:
    """Свежесть выгрузки из Apple Health — главный источник тихих поломок:
    автоматизация на телефоне отваливается молча."""
    try:
        rows = await repo.fetch(
            """
            SELECT max(day) AS last_day, max(updated_at) AS last_update
            FROM health_daily WHERE user_id = %s
            """,
            (user_id,),
        )
        last_day = rows[0]["last_day"] if rows else None
        last_update = rows[0]["last_update"] if rows else None
        if not last_day:
            return {"mark": WARN, "text": "Apple Health — выгрузок ещё не было"}

        stale = (date.today() - last_day).days
        upd = f", запись {last_update:%d.%m %H:%M}" if last_update else ""
        if stale <= 1:
            return {"mark": OK, "text": f"Apple Health — данные за {last_day:%d.%m}{upd}"}
        if stale <= 3:
            return {"mark": WARN, "text": f"Apple Health — {stale} дня без выгрузки (посл. {last_day:%d.%m})"}
        return {
            "mark": FAIL,
            "text": f"Apple Health — {stale} дней без выгрузки (посл. {last_day:%d.%m}). "
                    "Проверь автоматизацию на телефоне",
        }
    except Exception as e:
        return {"mark": FAIL, "text": f"Apple Health — ошибка запроса: {type(e).__name__}"}


async def check_api() -> dict[str, Any]:
    """FastAPI живёт в отдельном контейнере, поэтому стучимся по сети."""
    s = get_settings()
    url = f"http://api:{s.api_port}/healthz"
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            r = await client.get(url)
        if r.status_code == 200:
            return {"mark": OK, "text": f"Приём Apple Health (порт {s.api_port}) — отвечает"}
        return {"mark": WARN, "text": f"Приём Apple Health — код {r.status_code}"}
    except Exception:
        # локальный запуск без docker-сети: имя "api" не разрешается
        return {"mark": WARN, "text": "Приём Apple Health — не отвечает по имени api"}


async def data_stats(user_id: int) -> dict[str, Any]:
    """Что накопилось за неделю. «Всего за всё время» ничего не говорит
    о том, работает ли бот сейчас, — а неделя показывает живую картину."""
    try:
        rows = await repo.fetch(
            """
            SELECT
              (SELECT count(*) FROM meals
                 WHERE user_id = %(u)s
                   AND eaten_at > now() - interval '7 days')            AS meals_week,
              (SELECT count(*) FROM health_daily
                 WHERE user_id = %(u)s
                   AND day > (now() AT TIME ZONE 'Europe/Moscow')::date - 7
                   AND steps IS NOT NULL)                              AS days_week,
              (SELECT count(*) FROM messages
                 WHERE user_id = %(u)s AND role = 'user'
                   AND created_at > now() - interval '7 days')          AS msgs_week,
              (SELECT count(*) FROM my_foods WHERE user_id = %(u)s)     AS foods,
              (SELECT count(*) FROM lab_results WHERE user_id = %(u)s)  AS labs,
              (SELECT goal_text FROM goals
                 WHERE user_id = %(u)s AND is_active LIMIT 1)           AS goal,
              (SELECT max(eaten_at) FROM meals WHERE user_id = %(u)s)   AS last_meal,
              (SELECT max(updated_at) FROM health_daily
                 WHERE user_id = %(u)s)                                AS last_export
            """,
            {"u": user_id},
        )
        return rows[0] if rows else {}
    except Exception:
        log.exception("Не удалось собрать статистику")
        return {}


# ------------------------------------------------------------------ отчёт

async def report(user_id: int) -> str:
    """Отчёт о состоянии. Проверки параллельно: последовательно это заняло бы
    сумму всех таймаутов вместо максимального."""
    db, llm, vision, stt, api, export, profile, stats = await asyncio.gather(
        check_db(),
        check_llm(),
        check_vision(),
        check_stt(),
        check_api(),
        check_health_export(user_id),
        check_profile(user_id),
        data_stats(user_id),
        return_exceptions=True,
    )

    def line(res) -> str:
        if isinstance(res, Exception):
            return f"{FAIL} проверка упала: {type(res).__name__}"
        return f"{res['mark']} {res['text']}"

    s = get_settings()

    # Конфигурация первой строкой: понимать, на чём работает бот, важнее
    # до того, как читать проверки. Планировщик отсюда убран — его расписание
    # целиком описано в /start.
    parts = [
        "*Настройки*",
        "Все модели через *RouterAI*",
        f"текст и вопросы: `{s.routerai_llm_model}`",
        f"фото: `{s.routerai_vision_model}`",
        f"голос: `{s.routerai_stt_model}`",
        f"переспрашиваю вес, если уверенность ниже: "
        f"фото {s.threshold_photo} · голос {s.threshold_voice}",
        "",
        "*Сервисы*",
        *[line(x) for x in (db, api, llm, vision, stt, export, profile)],
    ]

    if isinstance(stats, dict) and stats:
        last_meal = stats.get("last_meal")
        last_export = stats.get("last_export")
        parts += [
            "",
            "*За последнюю неделю*",
            f"🍽 записей о еде: {stats.get('meals_week', 0)}",
            f"⌚ дней с данными Apple Health: {stats.get('days_week', 0)} из 7",
            f"💬 сообщений боту: {stats.get('msgs_week', 0)}",
            "",
            "*Всего накоплено*",
            f"продуктов в кэше: {stats.get('foods', 0)} · "
            f"показателей анализов: {stats.get('labs', 0)}",
            f"цель: {stats.get('goal') or 'не задана'}",
            "",
            "*Последнее обновление*",
            "еда: " + (f"{last_meal:%d.%m в %H:%M}" if last_meal else "записей нет"),
            "Apple Health: "
            + (f"{last_export:%d.%m в %H:%M}" if last_export else "выгрузок нет"),
        ]

    now = datetime.now(timezone.utc).astimezone()
    parts += ["", f"_Проверено {now:%d.%m %H:%M}_"]
    return "\n".join(parts)
