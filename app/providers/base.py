"""Абстракции провайдеров.

Смысл слоя: STT/vision/LLM меняются одной строкой в .env, а граф и хендлеры
про провайдера ничего не знают. Стартуем на Яндексе, при желании переключаемся
на OpenAI/Anthropic без правок логики.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class FoodItem(BaseModel):
    """Одна позиция, как её вернул распознаватель — ещё без нутриентов."""

    name: str = Field(description="название продукта или блюда на русском")
    grams: float | None = Field(default=None, description="вес порции в граммах")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class Recognition(BaseModel):
    """Результат любого канала ввода в едином формате."""

    items: list[FoodItem] = Field(default_factory=list)
    note: str | None = None

    @property
    def min_confidence(self) -> float:
        return min((i.confidence for i in self.items), default=0.0)

    @property
    def needs_grams(self) -> bool:
        return any(i.grams is None for i in self.items)


@runtime_checkable
class SttProvider(Protocol):
    async def transcribe(self, audio: bytes, mime: str = "audio/ogg") -> str: ...


@runtime_checkable
class VisionProvider(Protocol):
    async def recognize_food(self, image: bytes, caption: str | None = None) -> Recognition: ...


@runtime_checkable
class LlmProvider(Protocol):
    async def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...
