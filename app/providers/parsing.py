"""Разбор ответов LLM.

YandexGPT даже в json_object-режиме иногда оборачивает JSON в ```-блок или
добавляет пояснение до/после. Отдельный слой снимает эту боль и не даёт ей
расползтись по коду.
"""
from __future__ import annotations

import json
import logging
import re

from app.providers.base import FoodItem, Recognition

log = logging.getLogger(__name__)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> dict | list | None:
    """Вытаскивает первый валидный JSON из текста модели."""
    if not raw:
        return None

    candidates: list[str] = []
    if m := _FENCE.search(raw):
        candidates.append(m.group(1))
    candidates.append(raw)

    # запасной вариант: самый внешний {...} или [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if 0 <= start < end:
            candidates.append(raw[start : end + 1])

    for c in candidates:
        try:
            return json.loads(c.strip())
        except json.JSONDecodeError:
            continue

    log.warning("Не удалось распарсить JSON из ответа модели: %s", raw[:300])
    return None


def parse_recognition_json(raw: str) -> Recognition:
    """Приводит ответ модели к Recognition, не падая на кривом формате."""
    data = extract_json(raw)
    if data is None:
        return Recognition(items=[], note="не удалось разобрать ответ модели")

    # модель может вернуть либо {"items": [...]}, либо сразу список
    rows = data.get("items", []) if isinstance(data, dict) else data
    note = data.get("note") if isinstance(data, dict) else None

    items: list[FoodItem] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = (row.get("name") or row.get("продукт") or "").strip()
        if not name:
            continue
        items.append(
            FoodItem(
                name=name,
                grams=_as_float(row.get("grams") or row.get("граммы")),
                confidence=_as_float(row.get("confidence")) or 0.5,
            )
        )
    return Recognition(items=items, note=note)


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
