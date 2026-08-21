"""Форматирование сообщений."""
from __future__ import annotations

from app.services.tables import GREEN, ORANGE

CONF_MARK = {0.75: "", 0.5: " ~", 0.0: " ⚠️"}


def _conf_suffix(confidence: float | None) -> str:
    """Помечает позиции, в которых распознавание не уверено."""
    c = float(confidence if confidence is not None else 1.0)
    for threshold, mark in CONF_MARK.items():
        if c >= threshold:
            return mark
    return ""


def render_meal(items: list[dict], totals: dict, note: str | None = None) -> str:
    """Подтверждение распознанного приёма пищи.

    Структура: заголовок → позиции с весом и калориями → итог с БЖУ. Раньше всё
    шло сплошным списком, и в нём терялся итог: он выглядел такой же строкой,
    как позиции.
    """
    lines = []
    for it in items:
        name = it.get("resolved_name") or it["name"]
        per100 = it.get("kcal_100")
        # «115 ккал/100 г» — по этой цифре сразу видно, если модель дала сухую
        # крупу вместо отварной
        per100_part = f"\n   _{per100:.0f} ккал/100 г_" if per100 else ""
        lines.append(
            f"*{name}*{_conf_suffix(it.get('confidence'))}\n"
            f"   {it['grams']:.0f} г · *{it['kcal']:.0f} ккал*{per100_part}"
        )

    body = "\n\n".join(lines)
    total = (
        f"\n\n━━━━━━━━━━━━━━\n"
        f"*Итого {totals.get('kcal', 0):.0f} ккал*\n"
        f"Б {totals.get('protein', 0):.0f} г · "
        f"Ж {totals.get('fat', 0):.0f} г · "
        f"У {totals.get('carbs', 0):.0f} г"
    )
    head = "🍽 *Что записать*\n\n"
    tail = f"\n\n💬 _{note}_" if note else ""
    legend = ("\n\n_~ оценка приблизительная, ⚠️ низкая уверенность — "
              "поправь в «Исправить»_") if "~" in body or "⚠️" in body else ""
    return head + body + total + tail + legend


def day_table_data(row: dict | None, goals: list[dict],
                   health: dict | None = None,
                   label: str = "Сегодня") -> dict | None:
    """Данные для картинки со сводкой дня. None — если данных нет совсем.

    row — из v_daily_nutrition (наши записи), health — из v_daily_full
    (Apple Health плюс протянутый вес). Любая часть может отсутствовать:
    утром еды ещё нет, а выгрузка Health приходит вечером.

    Раньше это была ASCII-таблица с эмодзи в первой колонке, и эмодзи
    приходилось держать ВНЕ моноширинного блока: в нём они шире символа и
    ломали выравнивание. В картинке колонки выравниваются по пикселям, а
    эмодзи вставляются готовыми PNG (см. services/tables.py).
    """
    if not row and not health:
        return None

    h = health or {}

    def val(x, digits: int = 0, unit: str = "") -> str:
        if x is None:
            return "—"
        return f"{float(x):.{digits}f}{(' ' + unit) if unit else ''}"

    # Расход общий, но с расшифровкой на покой и активность — иначе непонятно,
    # откуда 2000 ккал у человека, который весь день сидел.
    active, resting = h.get("active_kcal"), h.get("resting_kcal")
    burned = None
    if active is not None or resting is not None:
        burned = float(active or 0) + float(resting or 0)

    eaten = float(row["kcal"]) if row and row.get("kcal") is not None else None
    balance = None
    if eaten is not None and burned is not None:
        balance = eaten - burned

    table_rows = [
        ["потратил калорий", val(burned, 0, "ккал")],
        ["съел калорий", val(eaten, 0, "ккал")],
        ["разница калорий", "—" if balance is None else f"{balance:+.0f} ккал"],
        ["вес", val(h.get("weight_kg"), 1, "кг")],
        ["сон", val(h.get("sleep_hours"), 1, "ч")],
        ["прошёл", val(h.get("distance_km"), 1, "км")],
    ]
    # Цветные иконки картинками из assets/emoji — matplotlib не рисует эмодзи
    # шрифтом, см. комментарий в services/tables.py
    icons = ["fire", "plate", "scales", "weight", "bed", "steps"]
    # разницу красим: дефицит зелёным, профицит оранжевым — при похудении это
    # главная строка, а знак «+» на телефоне легко проскочить
    row_colors: list[list[str | None]] = [[None, None] for _ in table_rows]
    if balance is not None:
        row_colors[2] = [None, GREEN if balance < 0 else ORANGE]

    footer = ""
    if not health:
        footer = "данные Apple Health за этот день ещё не пришли"
    elif eaten is not None and goals and goals[0].get("kcal_target"):
        target = goals[0]["kcal_target"]
        footer = f"цель {target} ккал, осталось {target - eaten:.0f}"

    return {
        "title": label,
        "header": ["", ""],          # без шапки: колонки и так понятны
        "rows": table_rows,
        "aligns": ["l", "r"],
        "colors": row_colors,
        "icons": icons,
        "footer": footer,
    }


def meals_table_data(meals: list[dict], label: str = "сегодня") -> dict:
    """Данные для картинки «что ел» — заголовок, строки, подпись.

    Раньше здесь собиралась ASCII-таблица, и всё упиралось в 30 символов
    ширины: названия резались до «гречка с кур…». Картинка от ширины экрана не
    зависит, поэтому состав отдаём целиком, а обрезаем только совсем длинный
    хвост — иначе одна запись из десяти позиций растянет таблицу на весь экран.
    """
    NAME_MAX = 38

    rows = []
    for m in meals:
        name = m.get("items") or "?"
        if len(name) > NAME_MAX:
            # режем по запятой, чтобы не рвать слово посреди продукта
            head = name[:NAME_MAX - 1]
            if "," in head:
                head = head.rsplit(",", 1)[0]
            name = head.rstrip(" ,") + "…"
        rows.append([f"{m['eaten_at']:%H:%M}", name, f"{float(m['kcal']):.0f}"])

    total = sum(float(m["kcal"]) for m in meals)
    word = "приём" if len(meals) == 1 else "приёма" if len(meals) < 5 else "приёмов"
    return {
        "title": f"Что ел {label}",
        "header": ["время", "что", "ккал"],
        "rows": rows,
        "aligns": ["l", "l", "r"],
        "head_icons": ["clock", "plate", "fire"],
        "footer": f"всего {total:.0f} ккал за {len(meals)} {word}",
    }
