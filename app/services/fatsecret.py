"""FatSecret Platform API — опциональный источник нутриентов.

Выключен, если не заданы FATSECRET_CLIENT_ID/SECRET: резолвер тогда просто
идёт в YandexGPT. Включать стоит после того, как проверишь руками, находится
ли по-русски то, что ты реально ешь.

Что проверено на живом API (тариф basic):
  * русский каталог недоступен — region/language работают только в scope
    premier, на basic «гречка» даёт 0 результатов. Русские названия уходят в LLM;
  * английские находятся, но порция почти никогда не 100 г: у Chicken Breast
    101 г, у брендов «Per 1 package» или «Per 7 chips» — отсюда пересчёт ниже;
  * ключи FatSecret при копировании с сайта тянут за собой неразрывный пробел
    (\xa0), и запрос падает с invalid_client. Если токен не берётся — первым
    делом проверь хвост значения в .env.

Ещё: FatSecret умеет ограничивать доступ по IP. Для VPS это ок, для локальной
разработки с динамическим адресом — источник неочевидных отказов.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.fatsecret.com/connect/token"
API_URL = "https://platform.fatsecret.com/rest/server.api"

_token: str | None = None
_token_expires_at: float = 0.0


def is_enabled() -> bool:
    return bool(os.environ.get("FATSECRET_CLIENT_ID") and os.environ.get("FATSECRET_CLIENT_SECRET"))


async def _get_token() -> str | None:
    global _token, _token_expires_at
    if _token and time.monotonic() < _token_expires_at:
        return _token

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials", "scope": "basic"},
                auth=(
                    os.environ["FATSECRET_CLIENT_ID"],
                    os.environ["FATSECRET_CLIENT_SECRET"],
                ),
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        log.exception("FatSecret: не удалось получить токен")
        return None

    _token = data["access_token"]
    _token_expires_at = time.monotonic() + int(data.get("expires_in", 3600)) - 60
    return _token


async def search_food(name: str) -> dict | None:
    """Ищет продукт и возвращает нутриенты на 100 г, либо None."""
    token = await _get_token()
    if not token:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                API_URL,
                params={
                    "method": "foods.search",
                    "search_expression": name,
                    "format": "json",
                    # больше кандидатов: у брендов первые записи часто идут
                    # порциями в штуках, из которых 100 г не посчитать
                    "max_results": 8,
                    # region/language не передаём: русский каталог доступен
                    # только в scope premier, на basic он молча игнорируется
                    # (проверено — «гречка» с region=RU даёт 0 результатов)
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            foods = r.json().get("foods", {}).get("food") or []
    except Exception:
        log.exception("FatSecret: ошибка поиска %r", name)
        return None

    if isinstance(foods, dict):
        foods = [foods]
    if not foods:
        return None

    # Перебираем все результаты, а не только первый: у брендовых продуктов
    # порция часто идёт как «Per 7 chips» — из такой записи 100 г не посчитать,
    # и раньше поиск возвращал None, хотя следующая запись годилась.
    #
    # TODO: описание нутриентов в foods.search приходит строкой
    # ("Per 100g - Calories: 132kcal | Fat: 0.53g ..."), парсер её разбирает
    # приблизительно. Точнее — дёрнуть food.get по food_id.
    for food in foods:
        if parsed := _parse_description(food):
            return parsed
    return None


def _parse_description(food: dict) -> dict | None:
    """Разбирает строку вида «Per 101g - Calories: 197kcal | Fat: 7.79g | …».

    Порция в описании почти никогда не ровно 100 г: у Chicken Breast это 101 г,
    у многих продуктов — чашки и штуки. Раньше здесь стояла проверка на «100g»,
    и всё остальное отбрасывалось — из-за неё FatSecret не находил даже базовые
    продукты. Теперь пересчитываем с фактической порции.

    Порции не в граммах (cup, oz, piece) всё равно отдаём в LLM: пересчитать
    «1 чашку» в граммы без плотности продукта нельзя.
    """
    import re

    desc = food.get("food_description", "")

    portion = re.search(r"Per\s+([\d.]+)\s*g\b", desc, re.IGNORECASE)
    grams = float(portion.group(1)) if portion else 0.0

    if grams <= 0:
        # Брендовые продукты почти всегда идут порцией «Per 1 package» или
        # «Per 7 chips» — граммов в описании нет, и раньше они отбрасывались
        # целиком: ни Lay's, ни Pringles не находились. Но вес обычно указан в
        # названии: «Classic Potato Chips (28.3g)» — берём его оттуда.
        in_name = re.search(r"\(([\d.]+)\s*g\)", food.get("food_name", ""),
                            re.IGNORECASE)
        if in_name:
            grams = float(in_name.group(1))

    if grams <= 0:
        # Порция в чашках или штуках без веса: пересчитать в 100 г нельзя,
        # отдаём в LLM — она хотя бы назовёт типовую калорийность.
        return None

    def grab(pattern: str) -> float:
        m = re.search(pattern, desc, re.IGNORECASE)
        return float(m.group(1)) if m else 0.0

    kcal = grab(r"Calories:\s*([\d.]+)")
    if not kcal:
        return None

    # Бренд в название: без него в подтверждении видно «Classic Potato Chips»
    # и непонятно, чьи именно чипсы нашлись — а от производителя зависят цифры.
    name = food.get("food_name", "")
    brand = (food.get("brand_name") or "").strip()
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}"

    # приводим к 100 г
    k = 100.0 / grams
    return {
        "canonical_name": name,
        "kcal_100": round(kcal * k, 1),
        "protein_100": round(grab(r"Protein:\s*([\d.]+)") * k, 1),
        "fat_100": round(grab(r"Fat:\s*([\d.]+)") * k, 1),
        "carbs_100": round(grab(r"Carbs:\s*([\d.]+)") * k, 1),
    }
