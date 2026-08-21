"""Раздел «Психолог»: состояние по объективным метрикам.

Работает на данных, а не на самоотчёте: ВСР, пульс покоя, фазы сна, дыхание.
Это то, что Apple Watch измеряет напрямую, и по этим цифрам видно накопленное
напряжение раньше, чем человек сам его замечает.

Что важно: раздел НЕ ставит диагнозов и не заменяет терапию — это прописано
в промпте, там же телефон психологической помощи на случай тяжёлого состояния.
"""
from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app import prompts
from app.db import repo
from app.handlers.keyboards import BTN_PSY
from app.services.tables import send_table
from app.providers.factory import get_llm

log = logging.getLogger(__name__)
router = Router(name="psy")

PSY_DAYS = 7


def _psy_table_data(rows: list[dict], cols: list[tuple[str, str, str]],
                    title: str) -> dict:
    """Данные для картинки по заданным колонкам (ключ, заголовок).

    В тексте это были две таблицы — «Сон» и «Пульс и активность»: десять
    колонок в 30 символов не влезали. В картинке всё умещается одной, и день
    читается целиком, без сопоставления двух блоков по дате.
    """
    out_rows = []
    for r in rows:
        cells = [f"{r['day']:%d.%m}"]
        for key, _, _ in cols:
            v = r.get(key)
            cells.append("—" if v is None else f"{float(v):g}")
        out_rows.append(cells)

    return {
        "title": title,
        "header": ["дата"] + [t for _, t, _ in cols],
        "rows": out_rows,
        "aligns": ["l"] + ["r"] * len(cols),
        "head_icons": ["date"] + [ic for _, _, ic in cols],
    }


async def show_psy(message: Message, user_id: int) -> None:
    """Таблица показателей за неделю, график и разбор от модели."""
    from app import metrics

    metrics.inc("sections", section="psy")
    from app.handlers.commands import section_trace

    async with section_trace("психолог", user_id):
        rows = await repo.fetch(
            """
            SELECT day, sleep_hours, hrv_ms, resting_hr, heart_rate_avg, steps,
                   round(kcal_eaten) AS eaten, round(kcal_burned) AS burned
            FROM v_daily_full
            WHERE user_id = %s
              AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
            ORDER BY day DESC
            """,
            (user_id, PSY_DAYS - 1),
        )
        if not rows:
            await message.answer(
                "Нет данных для разбора. Нужны хотя бы сон и пульс из Apple Health."
            )
            return

        status = await message.answer("Смотрю твои показатели…")

        # Колонки-прочерки не показываем: таблица из одних «—» только занимает
        # место и создаёт впечатление, что данные есть, но нулевые
        have = {k for r in rows for k, v in r.items() if v is not None}

        # третий элемент — имя цветной иконки над колонкой (services/tables.py)
        all_cols = [("sleep_hours", "сон\nчасов", "sleep"),
                    ("hrv_ms", "ВСР\nмс", "heart"),
                    ("resting_hr", "пульс покоя\nуд/мин", "heart"),
                    ("heart_rate_avg", "пульс средний\nуд/мин", "heart"),
                    ("steps", "шаги\nза день", "steps")]
        cols = [(k, t, ic) for k, t, ic in all_cols if k in have]

        missing = [
            label for key, label in (
                ("hrv_ms", "ВСР"), ("sleep_hours", "сон"), ("resting_hr", "пульс покоя"),
            ) if key not in have
        ]

        # статусное сообщение убираем: картинку в него не подменить, а «Смотрю
        # показатели…» рядом с готовой таблицей выглядит недоделанным
        await status.delete()
        if cols:
            data = _psy_table_data(rows, cols, f"Состояние за {PSY_DAYS} дней")
            if missing:
                data["footer"] = (f"не приходит: {', '.join(missing)} — "
                                  "добавь в Health Auto Export")
            await send_table(message, **data)
        elif missing:
            await message.answer(
                f"_Не приходит: {', '.join(missing)} — добавь эти метрики "
                "в Health Auto Export._", parse_mode="Markdown"
            )

        # График из раздела убран: те же семь дней уже есть в таблице выше, и
        # линии по четырём метрикам с разными единицами (часы, мс, уд/мин, шаги)
        # ничего к ней не добавляли. Раздел теперь короткий: таблица и разбор.

        def fmt(cols: list[str]) -> str:
            out = []
            for r in rows:
                vals = [f"{k}={r[k]}" for k in cols if r.get(k) is not None]
                if vals:
                    out.append(f"{r['day']:%d.%m} " + " ".join(vals))
            return "\n".join(out) or "нет данных"

        facts = await repo.get_facts(user_id)
        goals = await repo.get_active_goals(user_id)
        if goals:
            # цель влияет на трактовку: дефицит калорий при похудении — норма,
            # а при наборе массы — проблема
            facts = [f"цель: {goals[0]['goal_text']}"] + list(facts)
        note = ""
        if missing:
            note = f"\nЭти метрики не приходят: {', '.join(missing)}."

        try:
            text = await get_llm().complete(
                prompts.PSY_SYSTEM,
                prompts.PSY_USER.format(
                    days=PSY_DAYS,
                    sleep=fmt(["sleep_hours"]),
                    heart=fmt(["hrv_ms", "resting_hr", "heart_rate_avg"]),
                    activity=fmt(["steps", "eaten", "burned"]),
                    facts=", ".join(facts) if facts else "ничего",
                    note=note,
                ),
            )
        except Exception:
            log.exception("Модель не ответила на разбор состояния")
            await message.answer("Не смог разобрать состояние, попробуй позже.")
            return

        clean = re.sub(r"^#{1,6}\s*", "", text.strip(), flags=re.M).replace("**", "*")
        try:
            await message.answer(clean, parse_mode="Markdown")
        except Exception:
            await message.answer(clean)


@router.message(Command("psy"))
@router.message(F.text == BTN_PSY)
async def cmd_psy(message: Message) -> None:
    await show_psy(message, message.from_user.id)
