"""Состояние графа приёма пищи."""
from __future__ import annotations

from typing import Any, Literal, TypedDict

InputKind = Literal["photo", "voice", "text", "combined"]


class MealState(TypedDict, total=False):
    # вход
    user_id: int
    kind: InputKind
    image: bytes | None
    audio: bytes | None
    text: str | None
    caption: str | None
    photo_file_id: str | None

    # промежуточное
    transcript: str | None          # расшифровка голоса
    recognized: list[dict[str, Any]]  # FoodItem как dict — сериализуется в checkpointer
    note: str | None
    clarify_rounds: int             # защита от бесконечного цикла уточнений

    # результат
    resolved: list[dict[str, Any]]  # готовые позиции с нутриентами
    totals: dict[str, float]
    error: str | None
