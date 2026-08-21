"""Резолвер продуктов: название + граммы -> нутриенты.

Порядок источников:
  1. my_foods  — личный кэш, мгновенно и бесплатно;
  2. FatSecret — если ключ задан (см. fatsecret.py);
  3. YandexGPT — справочник последней надежды, результат кладётся в кэш.

Калории считаются здесь арифметикой из нутриентов на 100 г. Модель их не считает
никогда — это главное решение по точности во всём проекте.
"""
from __future__ import annotations

import logging

from app.db import repo
from app.providers.base import FoodItem
from app.providers.factory import get_llm
from app.providers.parsing import extract_json
from app.prompts import NUTRIENTS_SYSTEM, NUTRIENTS_USER
from app.services import fatsecret

log = logging.getLogger(__name__)

# Порция по умолчанию, когда вес неизвестен и уточнить не удалось
FALLBACK_GRAMS = 150.0


async def resolve_item(user_id: int, item: FoodItem) -> dict:
    """Возвращает готовую позицию для записи в meal_items."""
    per100, source = await _lookup(user_id, item.name)

    if per100 is None:
        # Всё сломалось (нет сети / модель не ответила). Не теряем запись —
        # сохраняем как unknown с нулями, пользователь поправит руками.
        log.warning("Не удалось определить нутриенты для %r", item.name)
        return _build(item, item.name, {"kcal_100": 0, "protein_100": 0,
                                       "fat_100": 0, "carbs_100": 0}, "unknown")

    return _build(item, per100["canonical_name"], per100, source)


async def _lookup(user_id: int, name: str) -> tuple[dict | None, str]:
    if cached := await repo.find_food(user_id, name):
        await repo.bump_hits(user_id, cached["alias"])
        # Персональный сканер: продукт, который человек уже ел (hits > 0),
        # получает его ПРИВЫЧНУЮ граммовку — она перебивает оценку модели
        # с фото. Ты ешь одни и те же бренды одинаковыми порциями.
        personal = (cached.get("hits") or 0) > 0
        return (
            {
                "canonical_name": cached["canonical_name"],
                "kcal_100": float(cached["kcal_100"]),
                "protein_100": float(cached["protein_100"]),
                "fat_100": float(cached["fat_100"]),
                "carbs_100": float(cached["carbs_100"]),
                "default_grams": cached["default_grams"],
                "force_default_grams": personal and cached["default_grams"] is not None,
            },
            "personal" if personal else "cache",
        )

    # FatSecret отключён. Проверка на пяти реальных продуктах (Lay's, творожок
    # Савушкин, Добрый кола, хлеб, борщ) дала 0 попаданий: русский каталог живёт
    # в платном scope premier, а US-база русских названий не знает. При этом на
    # каждый новый продукт добавлялось ~0.3 сек ожидания.
    # Чтобы вернуть: снять условие False и задать FATSECRET_CLIENT_ID/SECRET.
    if False and fatsecret.is_enabled():
        if found := await fatsecret.search_food(name):
            await repo.upsert_food(user_id, name, {**found, "source": "fatsecret"})
            return found, "fatsecret"

    if found := await _ask_llm(name):
        await repo.upsert_food(user_id, name, {**found, "source": "llm"})
        return found, "llm"

    return None, "unknown"


async def _ask_llm(name: str, use_web: bool = True) -> dict | None:
    """Нутриенты на 100 г: знания модели плюс выдержки из интернета.

    Поиск идёт подсказкой, а не истиной в последней инстанции: у фирменных
    продуктов («Яшкино крекер») в сети есть точные цифры, которых модель не
    знает, но выдача бывает и мусорной — поэтому модель сверяет её сама.
    Не нашлось ничего — отвечает как раньше, по памяти.
    """
    user = NUTRIENTS_USER.format(name=name)
    if use_web:
        try:
            from app.services import websearch
            ctx = await websearch.food_context(name)
            if ctx:
                user = f"{user}\n\n{ctx}"
        except Exception:
            log.exception("Поиск состава %r не удался — отвечаем по памяти", name)
    try:
        raw = await get_llm().complete(
            NUTRIENTS_SYSTEM, user, json_mode=True
        )
    except Exception:
        log.exception("Ошибка запроса нутриентов для %r", name)
        return None

    data = extract_json(raw)
    if not isinstance(data, dict) or "kcal_100" not in data:
        return None

    try:
        return {
            "canonical_name": str(data.get("canonical_name") or name),
            "kcal_100": float(data["kcal_100"]),
            "protein_100": float(data.get("protein_100", 0)),
            "fat_100": float(data.get("fat_100", 0)),
            "carbs_100": float(data.get("carbs_100", 0)),
        }
    except (TypeError, ValueError):
        log.warning("Нечисловые нутриенты в ответе модели: %s", data)
        return None


def _build(item: FoodItem, resolved_name: str, per100: dict, source: str) -> dict:
    # знакомый продукт → привычная порция важнее прикидки модели с фото
    if per100.get("force_default_grams"):
        grams = per100["default_grams"]
    else:
        grams = item.grams or per100.get("default_grams") or FALLBACK_GRAMS
    k = float(grams) / 100.0
    return {
        "name": item.name,
        "resolved_name": resolved_name,
        "grams": round(float(grams), 1),
        "kcal": round(per100["kcal_100"] * k, 1),
        "protein": round(per100["protein_100"] * k, 1),
        "fat": round(per100["fat_100"] * k, 1),
        "carbs": round(per100["carbs_100"] * k, 1),
        # Значения «на 100 г» тащим дальше целиком: kcal_100 показывается в
        # подтверждении (по ней видно, не подсунула ли модель сухой продукт
        # вместо готового), а все четыре нужны редактору в мини-аппе — он от них
        # пересчитывает порцию. Раньше здесь был только kcal_100, и поля БЖУ в
        # форме открывались пустыми, хотя данные уже были посчитаны.
        "kcal_100": round(per100["kcal_100"], 1),
        "protein_100": round(per100["protein_100"], 1),
        "fat_100": round(per100["fat_100"], 1),
        "carbs_100": round(per100["carbs_100"], 1),
        "confidence": item.confidence,
        "food_source": source,
    }
