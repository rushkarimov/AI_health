"""Свёртка больших выборок для модели.

Голая обрезка JSON до 6000 символов приводила к тому, что на вопрос про апрель
инструмент отдавал 152 дня, апрель попадал в отрезанный хвост, и агент отвечал
«нет данных» — хотя данные были.
"""
from datetime import date, timedelta
from decimal import Decimal

from app.graph.agent import _shrink


def _rows(n: int, start: date = date(2026, 3, 1)) -> list[dict]:
    """Свежие дни без пульса — как в реальной выгрузке."""
    out = []
    for i in range(n):
        day = start + timedelta(days=i)
        fresh = i >= n - 5
        out.append({
            "day": day,
            "steps": 8000 + i,
            "resting_hr": None if fresh else 52,
            "weight_kg": Decimal("90.5"),
        })
    return out


class TestShrink:
    def test_short_list_untouched(self):
        rows = _rows(10)
        assert _shrink(rows) == rows

    def test_long_list_becomes_summary(self):
        out = _shrink(_rows(152))
        assert "по_месяцам" in out
        assert "последние_дни" in out
        assert len(out["последние_дни"]) == 7

    def test_all_months_present(self):
        out = _shrink(_rows(152))
        months = {m["месяц"] for m in out["по_месяцам"]}
        # 152 дня с 1 марта покрывают март-июль: ни один месяц не теряется
        assert "2026-03" in months
        assert "2026-04" in months

    def test_column_none_in_first_row_still_aggregated(self):
        """resting_hr пуст в свежих днях; колонка определялась по первой строке
        и молча выпадала из агрегатов."""
        rows = sorted(_rows(152), key=lambda r: r["day"], reverse=True)
        assert rows[0]["resting_hr"] is None

        out = _shrink(rows)
        april = next(m for m in out["по_месяцам"] if m["месяц"] == "2026-04")
        assert "resting_hr_среднее" in april
        assert april["resting_hr_среднее"] == 52.0

    def test_averages_are_correct(self):
        out = _shrink(_rows(60))
        march = next(m for m in out["по_месяцам"] if m["месяц"] == "2026-03")
        assert march["дней_с_данными"] == 31
        assert march["steps_среднее"] == 8015.0
