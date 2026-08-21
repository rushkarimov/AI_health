"""Аналитический агент: вопросы к своей базе на естественном языке.

Реализован как ReAct-цикл руками, а не через create_react_agent, по одной
причине: у YandexGPT tool-calling работает менее предсказуемо, чем у OpenAI,
и надёжнее попросить его выбрать инструмент в JSON, чем полагаться на нативный
function calling. Когда переключишься на Claude/GPT — этот файл можно заменить
на create_react_agent(llm, tools) в три строки.

Инструменты — заранее написанные параметризованные запросы, а не свободный SQL.
Модель выбирает инструмент и период, но не сочиняет SQL: так она не сможет
ни сломать базу, ни тихо посчитать не то.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from app.db import repo
from app.providers.factory import get_llm
from app.providers.parsing import extract_json
from app import prompts
from app.prompts import AGENT_SYSTEM

log = logging.getLogger(__name__)

MAX_STEPS = 4


# --------------------------------------------------------------- инструменты

async def tool_nutrition(user_id: int, days: int = 7) -> list[dict]:
    """Питание по дням за последние N дней."""
    return await repo.fetch(
        """
        SELECT day, kcal, protein, fat, carbs, meals_count
        FROM v_daily_nutrition
        WHERE user_id = %s AND day >= %s
        ORDER BY day DESC
        """,
        (user_id, date.today() - timedelta(days=days)),
    )


async def tool_health(user_id: int, days: int = 7) -> list[dict]:
    """Данные Apple Health по дням."""
    return await repo.fetch(
        """
        SELECT day, steps, active_kcal, resting_kcal,
               -- итог отдаём явно: без него модель брала активность за весь
               -- расход и отвечала «потратил 458 ккал» вместо 2081
               total_kcal,
               distance_km, sleep_hours, resting_hr, weight_kg, body_fat_pct,
               dietary_kcal
        FROM health_daily
        WHERE user_id = %s AND day >= %s
        ORDER BY day DESC
        """,
        (user_id, date.today() - timedelta(days=days)),
    )


async def tool_balance(user_id: int, days: int = 7) -> list[dict]:
    """Баланс: съедено минус потрачено. Главная цифра для веса."""
    return await repo.fetch(
        """
        SELECT h.day,
               n.kcal AS eaten,
               (COALESCE(h.active_kcal, 0) + COALESCE(h.resting_kcal, 0)) AS burned,
               n.kcal - (COALESCE(h.active_kcal, 0) + COALESCE(h.resting_kcal, 0)) AS balance,
               h.weight_kg
        FROM health_daily h
        LEFT JOIN v_daily_nutrition n ON n.user_id = h.user_id AND n.day = h.day
        WHERE h.user_id = %s AND h.day >= %s
        ORDER BY h.day DESC
        """,
        (user_id, date.today() - timedelta(days=days)),
    )


async def tool_top_foods(user_id: int, days: int = 30) -> list[dict]:
    """Что ты ешь чаще всего и сколько это даёт калорий."""
    return await repo.fetch(
        """
        SELECT COALESCE(i.resolved_name, i.name) AS food,
               COUNT(*) AS times,
               ROUND(SUM(i.kcal)) AS total_kcal,
               ROUND(AVG(i.grams)) AS avg_grams
        FROM meal_items i
        JOIN meals m ON m.id = i.meal_id
        WHERE m.user_id = %s AND m.eaten_at >= %s
        GROUP BY food
        ORDER BY total_kcal DESC
        LIMIT 15
        """,
        (user_id, date.today() - timedelta(days=days)),
    )


async def tool_goals(user_id: int) -> list[dict]:
    """Текущие цели пользователя."""
    return await repo.get_active_goals(user_id)


async def tool_rag(user_id: int, query: str = "") -> list[dict]:
    """База знаний: научные статьи о питании, сне, анализах и тренировках."""
    from app.services import knowledge

    if not query:
        return []
    try:
        return await knowledge.search(query)
    except Exception:
        log.exception("База знаний недоступна")
        return []


async def tool_websearch(user_id: int, query: str = "") -> list[dict]:
    """Поиск в интернете: то, чего нет в базе и в знаниях модели."""
    from app.services import websearch

    if not query:
        return []
    return await websearch.search(query, count=4)


TOOLS = {
    "nutrition": (tool_nutrition, "питание по дням (ккал, БЖУ) за N дней"),
    "health": (tool_health, "шаги, сон, вес, процент жира, пульс, дистанция и "
                            "калории: total_kcal — ВЕСЬ расход за день, "
                            "active_kcal и resting_kcal — его слагаемые"),
    "balance": (tool_balance, "съедено минус потрачено по дням за N дней"),
    "top_foods": (tool_top_foods, "самые частые продукты за N дней"),
    "goals": (tool_goals, "цели пользователя (без параметров)"),
    "rag": (tool_rag,
                  "база научных статей: питание и белок при похудении, сон и "
                  "восстановление, нормы анализов, физическая активность. "
                  "Ищи здесь ДО интернета, когда вопрос про здоровье вообще, "
                  "а не про личные цифры пользователя. Параметр query — "
                  "поисковый запрос строкой"),
    "websearch": (tool_websearch,
                  "поиск в интернете: свежие факты, состав незнакомых "
                  "продуктов, новости науки о питании. Параметр query — "
                  "поисковый запрос строкой"),
}

_TOOLS_DESC = "\n".join(f"- {name}: {desc}" for name, (_, desc) in TOOLS.items())


def _tools_desc(web: bool, rag: bool = True) -> str:
    """Со снятой галочкой модель об инструменте просто не знает."""
    hidden = set()
    if not web:
        hidden.add("websearch")
    if not rag:
        hidden.add("rag")
    if not hidden:
        return _TOOLS_DESC
    return "\n".join(f"- {name}: {desc}" for name, (_, desc) in TOOLS.items()
                     if name not in hidden)


async def _db_today() -> date:
    """Сегодня по мнению базы, а не контейнера.

    Вьюхи группируют приёмы пищи по Europe/Moscow, а в контейнере UTC: ночью
    date.today() отставал на сутки, и модель не находила «сегодня» в выборке.
    """
    try:
        rows = await repo.fetch(
            "SELECT (now() AT TIME ZONE 'Europe/Moscow')::date AS d"
        )
        return rows[0]["d"]
    except Exception:
        log.exception("Не удалось взять дату из базы")
        return date.today()


try:
    from langsmith import traceable as _traceable
except ImportError:      # LangSmith не установлен — работаем без него
    def _traceable(*_a, **_kw):
        def deco(fn):
            return fn
        return deco


# Человеческие названия шагов: они уходят в интерфейс строкой под ответом,
# чтобы было видно, чем агент занят прямо сейчас.
STEP_TITLES = {
    "nutrition": "смотрю дневник питания",
    "health": "читаю метрики здоровья",
    "balance": "считаю баланс калорий",
    "top_foods": "поднимаю частые продукты",
    "goals": "сверяюсь с твоей целью",
    "rag": "смотрю в научных источниках",
    "websearch": "ищу в интернете",
}


@_traceable(name="agent-ask", run_type="chain")
async def ask(user_id: int, question: str,
              web: bool = True, rag: bool = True,
              on_step: Callable[[str], None] | None = None
              ) -> tuple[str, dict | None]:
    """ReAct-цикл: планируем -> вызываем инструмент -> повторяем -> отвечаем.

    on_step получает человекочитаемые статусы («ищу в интернете») — их
    показывает интерфейс, пока модель думает.
    """
    from app.services import memory

    llm = get_llm()
    today = await _db_today()
    # что знаем о человеке и о чём только что говорили: без этого «а сколько
    # это в белках?» не понять — предыдущей реплики в промпте не было
    context = await memory.build_context(user_id)
    chat_system = prompts.CHAT_AGENT_SYSTEM
    if context:
        chat_system += "\n\n" + context
    collected: dict[str, object] = {}
    seen: set[tuple[str, int]] = set()

    from app import tracing

    def say(text: str) -> None:
        if on_step:
            try:
                on_step(text)
            except Exception:
                log.exception("Статус шага не доставлен")

    say("думаю, что нужно посмотреть")
    for step in range(MAX_STEPS):
      async with tracing.trace_step(f"agent:шаг-{step + 1}"):
        plan_raw = await llm.complete(
            AGENT_SYSTEM,
            prompts.AGENT_PLAN_USER.format(
                question=question,
                today=today.strftime("%d.%m.%Y"),
                # ориентир: сколько дней назад началось начало года —
                # чтобы модель не боялась ставить большие days
                days_hint=(today - date(today.year, 4, 1)).days + 30,
                tools=_tools_desc(web, rag),
                collected=_dump(collected) or "пока ничего",
            ),
            json_mode=True,
        )
        plan = extract_json(plan_raw) or {}
        action = plan.get("action")
        tool_name = plan.get("tool")

        if action != "call" or tool_name not in TOOLS:
            break
        # страховка: модель иногда зовёт инструмент, которого ей не давали
        if tool_name == "websearch" and not web:
            break
        if tool_name == "rag" and not rag:
            break

        days = int(plan.get("days") or 7)
        # у поиска вместо периода — строка запроса, ей же различаем повторы
        query = str(plan.get("query") or "").strip()
        marker = (hash(query) % 100000
                  if tool_name in ("websearch", "rag") else days)
        if (tool_name, marker) in seen:
            break
        seen.add((tool_name, marker))

        title = STEP_TITLES.get(tool_name, tool_name)
        say(f"{title}: «{query}»" if tool_name == "websearch" and query else title)

        fn, _ = TOOLS[tool_name]
        async with tracing.trace_step(f"tool:{tool_name}", days=days) as fill:
            try:
                if tool_name == "goals":
                    rows = await fn(user_id)
                elif tool_name in ("websearch", "rag"):
                    rows = await fn(user_id, query or question)
                else:
                    rows = await fn(user_id, days)
            except Exception:
                log.exception("Ошибка инструмента %s", tool_name)
                rows = []
            # в Langfuse шаг был виден, но пустой: не понять, что искали
            # и что нашлось. Кладём запрос и краткую выжимку результата.
            if tool_name in ("websearch", "rag"):
                found = "\n".join(
                    f"• {r.get('title', '')}: {str(r.get('text', ''))[:160]}"
                    for r in rows[:3]) or "ничего не нашлось"
                fill(input=query or question,
                     output=f"нашлось источников: {len(rows)}\n{found}")
            else:
                fill(input=f"период: {days} дн.",
                     output=f"строк: {len(rows)}")
        key = f"{tool_name}:{query[:40]}" \
            if tool_name in ("websearch", "rag") else f"{tool_name}_{days}d"
        collected[key] = rows
        log.info("Агент: шаг %s, инструмент %s, строк %s", step + 1, tool_name, len(rows))

    if not collected:
        # Инструменты не понадобились: приветствие, благодарность, общий вопрос
        # про здоровье. Отвечаем словами и в базу не ходим — раньше здесь
        # насильно тянулись nutrition+health, и на «привет» бот присылал
        # таблицу за неделю.
        async with tracing.trace_step("agent:простой-ответ"):
            text = await llm.complete(
                chat_system, prompts.CHAT_USER.format(text=question)
            )
        return text, None

    say("собираю ответ")
    async with tracing.trace_step("agent:ответ"):
        answer = await llm.complete(
            chat_system,
            prompts.AGENT_ANSWER_USER.format(
                question=question,
                # ISO-формат: ровно так дата выглядит в JSON данных. С даты
                # "01.08.2026" модель не сопоставляла строку "2026-08-01" и
                # отвечала «данных нет», хотя они были первой строкой выборки.
                today=today.isoformat(),
                yesterday=(today - timedelta(days=1)).isoformat(),
                collected=_dump(collected),
            ),
        )
    # Таблицу отдаём отдельно от текста: хендлер отправит её картинкой, а
    # склеивать в одну строку нельзя — картинку в текст не вложить.
    return answer, _table_data(collected, question)


_DUMP_LIMIT = 6000
_ROWS_BEFORE_SUMMARY = 40


def _dump(data) -> str:
    """JSON для модели. Длинные выборки сначала сворачиваем в статистику по
    месяцам, и только потом ограничиваем длину.

    Раньше здесь была голая обрезка до 6000 символов: на вопрос про апрель
    инструмент отдавал 152 дня, апрель оказывался в отрезанном хвосте, и модель
    отвечала «нет данных» — при том что данные были.
    """
    if isinstance(data, dict):
        data = {k: _shrink(v) for k, v in data.items()}
    out = json.dumps(data, ensure_ascii=False, default=str, indent=1)
    if len(out) <= _DUMP_LIMIT:
        return out
    return out[:_DUMP_LIMIT] + "\n… вывод обрезан"


def _shrink(rows):
    """Много дней -> месячные агрегаты плюс последняя неделя подробно."""
    if not isinstance(rows, list) or len(rows) <= _ROWS_BEFORE_SUMMARY:
        return rows
    if not rows or not isinstance(rows[0], dict) or "day" not in rows[0]:
        return rows[:_ROWS_BEFORE_SUMMARY]

    by_month: dict[str, list[dict]] = {}
    for r in rows:
        day = r.get("day")
        key = f"{day:%Y-%m}" if isinstance(day, date) else str(day)[:7]
        by_month.setdefault(key, []).append(r)

    # колонки собираем по всей выборке, а не по первой строке: в свежих днях
    # половина метрик ещё None, и такие колонки молча выпадали из агрегатов
    numeric = [
        k
        for k in rows[0]
        if k != "day"
        and any(isinstance(r.get(k), (int, float, Decimal)) for r in rows)
    ]
    months = []
    for month, group in sorted(by_month.items()):
        stat = {"месяц": month, "дней_с_данными": len(group)}
        for col in numeric:
            vals = [float(r[col]) for r in group if r.get(col) is not None]
            if vals:
                stat[f"{col}_среднее"] = round(sum(vals) / len(vals), 1)
        months.append(stat)

    # последние 7 дней оставляем как есть: свежие цифры часто и есть ответ
    recent = sorted(rows, key=lambda r: r["day"], reverse=True)[:7]
    return {"по_месяцам": months, "последние_дни": recent}


# Заголовки для таблицы: короткие, чтобы строка влезала в ширину экрана телефона.
_TABLE_COLS = {
    "day": "дата",
    "kcal": "ккал",
    "kcal_eaten": "съел",
    "dietary_kcal": "съел(H)",
    "protein": "белк",
    "fat": "жир",
    "carbs": "угл",
    "active_kcal": "актив",
    "resting_kcal": "покой",
    "total_kcal": "потрач",
    "burned": "потрач",
    "balance": "баланс",
    "steps": "шаги",
    "distance_km": "км",
    "sleep_hours": "сон",
    "weight_kg": "вес",
    "body_fat_pct": "жир%",
    "resting_hr": "пульс",
}
_TABLE_MAX_ROWS = 14
_TABLE_TITLE = "По твоим данным"

# Цветная иконка над колонкой. Имена — из services/tables.py EMOJI.
_COL_ICONS = {
    "day": "date",
    "kcal": "plate", "kcal_eaten": "plate", "dietary_kcal": "plate",
    "protein": "plate", "fat": "plate", "carbs": "plate",
    "active_kcal": "fire", "resting_kcal": "fire",
    "total_kcal": "fire", "burned": "fire",
    "balance": "scales",
    "steps": "steps", "distance_km": "steps",
    "sleep_hours": "bed",
    "weight_kg": "weight", "body_fat_pct": "weight",
    "resting_hr": "heart", "heart_rate_avg": "heart", "hrv_ms": "heart",
}


# Метрики, где важна десятая доля: вес 80.5 и 80 — разные вещи, а ккал 1850.4
# округлить не жалко. Округление по величине здесь не работает.
_DECIMAL_COLS = {"weight_kg", "body_fat_pct", "sleep_hours", "distance_km", "lean_body_mass_kg"}


def _fmt_cell(value, col: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, date):
        return f"{value:%d.%m}"
    if isinstance(value, (int, Decimal, float)):
        num = float(value)
        return f"{num:.1f}" if col in _DECIMAL_COLS else f"{num:.0f}"
    return str(value)[:12]


# Что вытащить вперёд, если человек спросил именно об этом. Без приоритета
# колонки шли фиксированным порядком, и на вопрос «сколько шагов вчера?»
# обрезка по ширине оставляла «съел / актив / покой», а шаги выкидывала.
_COL_HINTS = {
    "шаг": ("steps",),
    "прошел": ("steps", "distance_km"),
    "прошёл": ("steps", "distance_km"),
    "дистанц": ("distance_km", "steps"),
    "км": ("distance_km", "steps"),
    "сон": ("sleep_hours",),
    "спал": ("sleep_hours",),
    "вес": ("weight_kg", "body_fat_pct"),
    "процент жира": ("body_fat_pct", "weight_kg"),
    "жира": ("body_fat_pct", "weight_kg"),
    "пульс": ("resting_hr",),
    "белк": ("protein", "kcal"),
    "жиров": ("fat", "kcal"),
    "углев": ("carbs", "kcal"),
    "съел": ("kcal", "dietary_kcal"),
    "потрат": ("total_kcal", "active_kcal"),
    "потрач": ("total_kcal", "active_kcal"),
    "актив": ("active_kcal", "steps"),
    "баланс": ("balance", "kcal", "total_kcal"),
    "калор": ("kcal", "total_kcal"),
}


def _priority_cols(question: str) -> list[str]:
    """Колонки, которые обязательно должны попасть в таблицу по смыслу вопроса."""
    q = (question or "").lower()
    out: list[str] = []
    for word, cols in _COL_HINTS.items():
        if word in q:
            for c in cols:
                if c not in out:
                    out.append(c)
    return out


def _table_data(collected: dict[str, object], question: str = "") -> dict | None:
    """Данные для картинки-таблицы по самой длинной собранной выборке.

    Модель отвечает текстом, но по цифрам за период глазом удобнее смотреть
    таблицу, поэтому отдаём и то и другое: текст сообщением, таблицу фото.

    Раньше здесь собиралась ASCII-таблица в моноширинном блоке, и всё упиралось
    в 42 символа ширины — колонки приходилось выбрасывать. У картинки такого
    ограничения нет.
    """
    best: list[dict] = []
    for rows in collected.values():
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            if len(rows) > len(best):
                best = rows
    if len(best) < 2:
        return None

    cols = [c for c in _TABLE_COLS if c in best[0]]
    if len(cols) < 2:
        return None

    # дата всегда первая, затем спрошенное, затем остальное
    wanted = [c for c in _priority_cols(question) if c in cols]
    if wanted:
        cols = ["day"] + wanted + [c for c in cols if c != "day" and c not in wanted]

    # Выборка отсортирована по убыванию даты, и голое best[:14] всегда давало
    # последние дни: на вопрос про апрель под ответом висел июль с прочерками.
    # Берём самые заполненные дни, потом возвращаем их в хронологический порядок.
    def filled(row: dict) -> int:
        return sum(1 for c in cols if row.get(c) is not None)

    top = sorted(best, key=lambda r: (-filled(r), r["day"]))[:_TABLE_MAX_ROWS]
    rows = sorted(top, key=lambda r: r["day"], reverse=True)
    body = [[_fmt_cell(r.get(c), c) for c in cols] for r in rows]

    footer = ""
    if len(best) > len(rows):
        footer = f"показано {len(rows)} самых полных дней из {len(best)}"

    return {
        "title": _TABLE_TITLE,
        "header": [_TABLE_COLS[c] for c in cols],
        "rows": body,
        "aligns": ["l"] + ["r"] * (len(cols) - 1),
        "head_icons": [_COL_ICONS.get(c) for c in cols],
        "footer": footer,
    }


