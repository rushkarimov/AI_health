"""Тесты маршрутизации графа.

Ключевая проверка: порог уточнения у фото строже, чем у голоса. Одна и та же
уверенность 0.6 для фото — повод спросить вес, для голоса — нет.
"""
from __future__ import annotations

import os

import pytest

# порог должен быть выставлен до первого get_settings() (он закэширован)
os.environ.setdefault("CONFIDENCE_THRESHOLD_PHOTO", "0.75")
os.environ.setdefault("CONFIDENCE_THRESHOLD_VOICE", "0.5")

from app.config import get_settings  # noqa: E402
from app.graph.meal_graph import (  # noqa: E402
    MAX_CLARIFY_ROUNDS,
    _extract_grams,
    _needs_clarification,
    route_entry,
)


def item(name="плов", grams=200.0, confidence=0.9) -> dict:
    return {"name": name, "grams": grams, "confidence": confidence}


class TestRouteEntry:
    def test_photo_goes_to_vision(self):
        assert route_entry({"kind": "photo"}) == "vision"

    def test_voice_goes_to_stt(self):
        assert route_entry({"kind": "voice"}) == "stt"

    def test_text_goes_to_parse(self):
        assert route_entry({"kind": "text"}) == "parse"

    def test_pretranscribed_voice_skips_stt(self):
        # хендлер расшифровал голос сам, чтобы отличить вопрос от еды —
        # второй раз STT гонять не надо
        assert route_entry({"kind": "voice", "text": "гречка 200 грамм"}) == "parse"

    def test_voice_without_text_still_goes_to_stt(self):
        assert route_entry({"kind": "voice", "audio": b"..."}) == "stt"

    def test_default_is_parse(self):
        assert route_entry({}) == "parse"


class TestNeedsClarification:
    def test_confident_item_passes(self):
        state = {"kind": "voice", "recognized": [item()]}
        assert _needs_clarification(state) is False

    def test_missing_grams_always_asks(self):
        state = {"kind": "voice", "recognized": [item(grams=None)]}
        assert _needs_clarification(state) is True

    def test_low_confidence_no_longer_asks(self):
        """Порог уверенности убран: неуверенные позиции показываем сразу.

        Раньше фото с confidence ниже 0.75 уходило в уточнение, и вместо
        результата человек получал «уточни вес», не видя, что бот распознал.
        Теперь такие позиции приходят с предположенной граммовкой, а поправить
        их можно в редакторе.
        """
        recognized = [item(confidence=0.2)]
        assert _needs_clarification({"kind": "photo", "recognized": recognized}) is False
        assert _needs_clarification({"kind": "voice", "recognized": recognized}) is False

    def test_stops_after_max_rounds(self):
        """Защита от бесконечного цикла: не спрашиваем вечно."""
        state = {
            "kind": "photo",
            "recognized": [item(grams=None)],
            "clarify_rounds": MAX_CLARIFY_ROUNDS,
        }
        assert _needs_clarification(state) is False

    def test_empty_recognized_does_not_ask(self):
        assert _needs_clarification({"kind": "photo", "recognized": []}) is False

    def test_one_item_without_grams_triggers(self):
        """Спрашиваем, только если у позиции нет веса вообще."""
        state = {"kind": "voice", "recognized": [item(), item(name="соус", grams=None)]}
        assert _needs_clarification(state) is True


class TestExtractGrams:
    def test_comma_separated(self):
        assert _extract_grams("200, 150") == [200.0, 150.0]

    def test_with_units(self):
        assert _extract_grams("200 г и 150 грамм") == [200.0, 150.0]

    def test_decimal_comma(self):
        assert _extract_grams("12,5") == [12.5]

    def test_no_numbers(self):
        assert _extract_grams("не знаю") == []

    def test_none(self):
        assert _extract_grams(None) == []


class TestThresholds:
    def test_threshold_for_photo_and_voice(self):
        s = get_settings()
        assert s.threshold_for("photo") > s.threshold_for("voice")


class TestSchedulerConstants:
    """EXPECTED_YESTERDAY хранит ключи METRICS, а не имена колонок.
    Расхождение уже ловилось один раз: 'active_kcal' вместо 'active' давало
    KeyError на каждом запуске алерта о пропаже данных."""

    def test_expected_yesterday_keys_exist(self):
        from app.db.repo import METRICS
        from app.scheduler import EXPECTED_YESTERDAY

        assert EXPECTED_YESTERDAY
        for key in EXPECTED_YESTERDAY:
            assert key in METRICS, f"{key} нет в METRICS"


class TestPeriodParsing:
    def test_parses_two_dates(self):
        from app.handlers.commands import parse_period

        assert parse_period("01.07 - 15.07") is not None

    def test_reversed_dates_are_sorted(self):
        from app.handlers.commands import parse_period

        start, end = parse_period("15.07-01.07")
        assert start < end

    def test_food_text_is_not_a_period(self):
        from app.handlers.commands import parse_period

        assert parse_period("гречка 200 грамм") is None
        assert parse_period("200, 150") is None

    def test_invalid_date_rejected(self):
        from app.handlers.commands import parse_period

        assert parse_period("31.02 15.03") is None
