"""Разбор того, что реально присылают «Быстрые команды».

Shortcuts на русской локали отдают 7,5 вместо 7.5 и пустую строку для метрики,
которой за день не было. Оба случая раньше валили запрос с 422, то есть ночная
выгрузка ломалась молча.
"""
from app.api import HealthPayload


class TestRussianLocaleNumbers:
    def test_comma_decimal(self):
        p = HealthPayload(user_id=1, sleep_hours="7,5", weight_kg="80,3")
        assert p.sleep_hours == 7.5
        assert p.weight_kg == 80.3

    def test_dot_decimal_still_works(self):
        assert HealthPayload(user_id=1, weight_kg="80.3").weight_kg == 80.3

    def test_numbers_as_strings(self):
        p = HealthPayload(user_id="123456789", steps="9800")
        assert p.user_id == 123456789
        assert p.steps == 9800


class TestEmptyValues:
    def test_empty_string_becomes_none(self):
        p = HealthPayload(user_id=1, active_kcal="", weight_kg="  ")
        assert p.active_kcal is None
        assert p.weight_kg is None

    def test_omitted_fields_are_none(self):
        assert HealthPayload(user_id=1).steps is None


class TestGarbageRejected:
    def test_text_is_not_a_number(self):
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HealthPayload(user_id=1, steps="много")


class TestUnitConversion:
    """Health Auto Export отдаёт единицы из системных настроек iPhone.
    На нашем телефоне энергия пришла в кДж: активность выглядела как 1915
    вместо 458 ккал, и это попало бы в базу как есть."""

    def test_kilojoules_to_kcal(self):
        from app.api import _convert_units

        assert round(_convert_units("active_kcal", 1915.4, "kj")) == 458
        assert round(_convert_units("resting_kcal", 6789.5, "kJ".lower())) == 1623

    def test_kcal_stays_kcal(self):
        from app.api import _convert_units

        assert _convert_units("active_kcal", 458.0, "kcal") == 458.0

    def test_miles_to_km(self):
        from app.api import _convert_units

        assert round(_convert_units("distance_km", 4.6, "mi"), 1) == 7.4

    def test_pounds_to_kg(self):
        from app.api import _convert_units

        assert round(_convert_units("weight_kg", 203.3, "lb"), 1) == 92.2

    def test_body_fat_fraction_to_percent(self):
        from app.api import _convert_units

        assert round(_convert_units("body_fat_pct", 0.271, "%"), 1) == 27.1
        # уже в процентах — не трогаем
        assert _convert_units("body_fat_pct", 27.1, "%") == 27.1

    def test_sleep_minutes_to_hours(self):
        from app.api import _convert_units

        assert _convert_units("sleep_hours", 450.0, "min") == 7.5


class TestSleepParsing:
    """У сна нет qty — Health Auto Export отдаёт разбивку по фазам, поэтому
    за 212 дней в базу не попало ни одного значения сна."""

    def test_phases_summed_without_in_bed(self):
        from app.api import _sleep_hours

        # inBed не входит в сумму: это «лёг», а не «спал»
        p = {"deep": 1.2, "core": 4.1, "rem": 1.3, "inBed": 8.4}
        assert _sleep_hours(p, "hr") == 6.6

    def test_flat_qty(self):
        from app.api import _sleep_hours

        assert _sleep_hours({"qty": 7.5}, "hr") == 7.5

    def test_minutes_converted(self):
        from app.api import _sleep_hours

        assert _sleep_hours({"asleep": 450}, "min") == 7.5

    def test_minutes_detected_by_magnitude(self):
        from app.api import _sleep_hours

        # больше 24 не может быть часами — значит минуты
        assert _sleep_hours({"qty": 450}, "hr") == 7.5

    def test_empty_point(self):
        from app.api import _sleep_hours

        assert _sleep_hours({"date": "2026-07-01"}, "hr") is None

    def test_absurd_value_rejected(self):
        from app.api import _sleep_hours

        assert _sleep_hours({"qty": 0}, "hr") is None
