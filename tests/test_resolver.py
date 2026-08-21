"""Тесты арифметики нутриентов и нормализации алиасов.

Проверяем именно то, ради чего модель отстранена от подсчёта калорий:
пересчёт на граммы должен быть точным и предсказуемым.
"""
from __future__ import annotations

from app.db.repo import normalize_alias
from app.providers.base import FoodItem
from app.services.resolver import FALLBACK_GRAMS, _build


PER100 = {
    "canonical_name": "Гречка отварная",
    "kcal_100": 110.0,
    "protein_100": 4.2,
    "fat_100": 1.1,
    "carbs_100": 21.3,
}


class TestBuild:
    def test_scales_to_grams(self):
        row = _build(FoodItem(name="гречка", grams=200), "Гречка отварная", PER100, "cache")
        assert row["kcal"] == 220.0
        assert row["protein"] == 8.4
        assert row["grams"] == 200.0

    def test_half_portion(self):
        row = _build(FoodItem(name="гречка", grams=50), "Гречка отварная", PER100, "cache")
        assert row["kcal"] == 55.0

    def test_uses_default_grams_when_unknown(self):
        per100 = {**PER100, "default_grams": 180}
        row = _build(FoodItem(name="гречка", grams=None), "Гречка", per100, "cache")
        assert row["grams"] == 180.0
        assert row["kcal"] == 198.0

    def test_fallback_grams_when_nothing_known(self):
        row = _build(FoodItem(name="гречка", grams=None), "Гречка", PER100, "llm")
        assert row["grams"] == FALLBACK_GRAMS

    def test_keeps_confidence_and_source(self):
        row = _build(
            FoodItem(name="плов", grams=300, confidence=0.35), "Плов", PER100, "llm"
        )
        assert row["confidence"] == 0.35
        assert row["food_source"] == "llm"


class TestNormalizeAlias:
    def test_lowercases_and_trims(self):
        assert normalize_alias("  Гречка Отварная  ") == "гречка отварная"

    def test_collapses_spaces(self):
        assert normalize_alias("куриная    грудка") == "куриная грудка"

    def test_yo_to_e(self):
        assert normalize_alias("Творог обезжирённый") == normalize_alias("творог обезжиренный")
