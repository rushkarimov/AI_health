"""Тесты разбора ответов LLM — самое хрупкое место, ломается чаще всего."""
from __future__ import annotations

from app.providers.parsing import extract_json, parse_recognition_json


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        raw = 'Вот результат:\n```json\n{"a": 1}\n```\nГотово.'
        assert extract_json(raw) == {"a": 1}

    def test_json_with_prose_around(self):
        raw = 'Конечно! {"items": []} Надеюсь, помог.'
        assert extract_json(raw) == {"items": []}

    def test_broken_returns_none(self):
        assert extract_json("это не json вообще") is None

    def test_empty_returns_none(self):
        assert extract_json("") is None


class TestParseRecognition:
    def test_standard_shape(self):
        raw = '{"items": [{"name": "гречка", "grams": 200, "confidence": 0.9}]}'
        rec = parse_recognition_json(raw)
        assert len(rec.items) == 1
        assert rec.items[0].name == "гречка"
        assert rec.items[0].grams == 200
        assert rec.min_confidence == 0.9

    def test_bare_list(self):
        raw = '[{"name": "яблоко", "grams": 180}]'
        rec = parse_recognition_json(raw)
        assert rec.items[0].name == "яблоко"

    def test_russian_keys(self):
        raw = '{"items": [{"продукт": "борщ", "граммы": "300"}]}'
        rec = parse_recognition_json(raw)
        assert rec.items[0].name == "борщ"
        assert rec.items[0].grams == 300

    def test_null_grams_flags_needs_grams(self):
        raw = '{"items": [{"name": "плов", "grams": null, "confidence": 0.4}]}'
        rec = parse_recognition_json(raw)
        assert rec.needs_grams is True

    def test_comma_decimal(self):
        raw = '{"items": [{"name": "масло", "grams": "12,5"}]}'
        rec = parse_recognition_json(raw)
        assert rec.items[0].grams == 12.5

    def test_skips_items_without_name(self):
        raw = '{"items": [{"grams": 100}, {"name": "рис", "grams": 150}]}'
        rec = parse_recognition_json(raw)
        assert len(rec.items) == 1

    def test_broken_json_returns_empty_with_note(self):
        rec = parse_recognition_json("модель сломалась")
        assert rec.items == []
        assert rec.note
