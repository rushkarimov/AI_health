"""Работа с данными. Весь SQL живёт здесь."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.db.pool import get_pool

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- кэш продуктов

def normalize_alias(name: str) -> str:
    """Ключ кэша. Простая нормализация: регистр, пробелы, ё."""
    return " ".join(name.lower().replace("ё", "е").split())


async def find_food(user_id: int, name: str) -> dict[str, Any] | None:
    """Ищет продукт в личном кэше — точное совпадение, затем похожее."""
    alias = normalize_alias(name)
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM my_foods WHERE user_id = %s AND alias = %s", (user_id, alias)
            )
            if row := await cur.fetchone():
                return row

            # Нестрогий поиск. Сортируем СНАЧАЛА по близости названия и только
            # потом по популярности: иначе частый «чипсы lay's сыр» (8 хитов)
            # выигрывал у запроса «сыр» у настоящего «сыр ломтик» (5 хитов),
            # и вместо сыра в дневник попадали чипсы впятеро калорийнее.
            await cur.execute(
                """
                SELECT *,
                    CASE
                        -- запрос — отдельное слово в начале алиаса: «сыр» → «сыр ломтик»
                        WHEN alias LIKE %(w)s || ' %%' THEN 0
                        -- алиас целиком входит в запрос: «кола» → «добрый кола 0.5»
                        WHEN %(w)s LIKE '%%' || alias || '%%' THEN 1
                        -- запрос — отдельное слово в середине или конце
                        WHEN alias LIKE '%% ' || %(w)s OR alias LIKE '%% ' || %(w)s || ' %%' THEN 2
                        -- просто подстрока: «сыр» внутри «сырники», «со вкусом сыра»
                        ELSE 3
                    END AS closeness
                FROM my_foods
                WHERE user_id = %(u)s
                  AND (alias LIKE '%%' || %(w)s || '%%' OR %(w)s LIKE '%%' || alias || '%%')
                ORDER BY closeness, hits DESC
                LIMIT 1
                """,
                {"u": user_id, "w": alias},
            )
            return await cur.fetchone()


async def upsert_food(user_id: int, alias: str, data: dict[str, Any]) -> None:
    """Кладёт продукт в кэш. Следующий раз найдётся мгновенно и без токенов."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO my_foods
                (user_id, alias, canonical_name, kcal_100, protein_100, fat_100,
                 carbs_100, default_grams, source, hits)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (user_id, alias) DO UPDATE SET
                canonical_name = EXCLUDED.canonical_name,
                kcal_100 = EXCLUDED.kcal_100,
                protein_100 = EXCLUDED.protein_100,
                fat_100 = EXCLUDED.fat_100,
                carbs_100 = EXCLUDED.carbs_100,
                default_grams = COALESCE(EXCLUDED.default_grams, my_foods.default_grams),
                hits = my_foods.hits + 1,
                updated_at = now()
            """,
            (
                user_id,
                normalize_alias(alias),
                data["canonical_name"],
                data["kcal_100"],
                data.get("protein_100", 0),
                data.get("fat_100", 0),
                data.get("carbs_100", 0),
                data.get("default_grams"),
                data.get("source", "llm"),
            ),
        )


async def bump_hits(user_id: int, alias: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE my_foods SET hits = hits + 1 WHERE user_id = %s AND alias = %s",
            (user_id, normalize_alias(alias)),
        )


# ------------------------------------------------------------------ приёмы пищи

async def save_meal(
    user_id: int,
    source: str,
    items: list[dict[str, Any]],
    raw_input: str | None = None,
    photo_file_id: str | None = None,
    comment: str | None = None,
    eaten_local: str | None = None,
) -> int:
    """Сохраняет приём пищи вместе с позициями. Возвращает meal_id."""
    # Метрика здесь, а не в хендлере: записать еду можно двумя путями —
    # кнопкой «Записать» в чате и сохранением из мини-аппа. Раньше счётчик
    # стоял только в первом, и записи из редактора не считались вовсе.
    from app import metrics

    metrics.inc("meals", source=source)

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # eaten_local — «2026-08-08 09:00» по Москве: запись в конкретный
            # приём прошлого дня из журнала, а не «сейчас»
            await cur.execute(
                """
                INSERT INTO meals (user_id, source, raw_input, photo_file_id,
                                   comment, eaten_at)
                VALUES (%s, %s, %s, %s, %s,
                        COALESCE(%s::timestamp AT TIME ZONE 'Europe/Moscow', now()))
                RETURNING id
                """,
                (user_id, source, raw_input, photo_file_id, comment, eaten_local),
            )
            meal_id = (await cur.fetchone())[0]

            for it in items:
                await cur.execute(
                    """
                    INSERT INTO meal_items
                        (meal_id, name, resolved_name, grams, kcal, protein, fat,
                         carbs, confidence, food_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        meal_id,
                        it["name"],
                        it.get("resolved_name"),
                        it["grams"],
                        it["kcal"],
                        it.get("protein", 0),
                        it.get("fat", 0),
                        it.get("carbs", 0),
                        it.get("confidence"),
                        it.get("food_source"),
                    ),
                )

            # Каталог учится на КАЖДОЙ записи (бот, мини-апп, чат-клиенты):
            # частые блюда всплывают в подсказках первыми, а последняя
            # граммовка становится привычной порцией для сканера.
            for it in items:
                name = (it.get("resolved_name") or it["name"] or "").strip()
                if not name or float(it.get("kcal_100") or 0) <= 0:
                    continue
                await cur.execute(
                    """
                    INSERT INTO my_foods (user_id, alias, canonical_name,
                                          kcal_100, protein_100, fat_100,
                                          carbs_100, default_grams, source, hits)
                    VALUES (%s, lower(%s), %s, %s, %s, %s, %s, %s, 'manual', 1)
                    ON CONFLICT (user_id, alias) DO UPDATE SET
                        hits = my_foods.hits + 1,
                        default_grams = EXCLUDED.default_grams,
                        kcal_100 = EXCLUDED.kcal_100,
                        protein_100 = EXCLUDED.protein_100,
                        fat_100 = EXCLUDED.fat_100,
                        carbs_100 = EXCLUDED.carbs_100,
                        updated_at = now()
                    """,
                    (user_id, name, name,
                     it.get("kcal_100") or 0, it.get("protein_100") or 0,
                     it.get("fat_100") or 0, it.get("carbs_100") or 0,
                     it.get("grams") or 100),
                )
    return meal_id


async def delete_meal(user_id: int, meal_id: int) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM meals WHERE id = %s AND user_id = %s", (meal_id, user_id)
            )
            return cur.rowcount > 0


async def last_meals(user_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """Последние приёмы пищи — для кнопки «повторить»."""
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT m.id, m.eaten_at, m.source,
                       ROUND(SUM(i.kcal)) AS kcal,
                       string_agg(COALESCE(i.resolved_name, i.name), ', '
                                  ORDER BY i.id) AS items
                FROM meals m
                JOIN meal_items i ON i.meal_id = m.id
                WHERE m.user_id = %s
                GROUP BY m.id
                ORDER BY m.eaten_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            return await cur.fetchall()


async def meal_items(meal_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM meal_items WHERE meal_id = %s ORDER BY id", (meal_id,)
            )
            return await cur.fetchall()


# --------------------------------------------------------------- Apple Health

HEALTH_FIELDS = (
    "steps",
    "active_kcal",
    "resting_kcal",
    "distance_km",
    "sleep_hours",
    "resting_hr",
    "weight_kg",
    # с умных весов
    "body_fat_pct",
    # питание по данным Apple Health — независимо от meals
    "dietary_kcal",
    # ВСР: главный маркер стресса, нужен «Психологу» и алерту
    "heart_rate_avg",
    "hrv_ms",
    "bmi",
    # частота дыхания во сне — из Google Health (Fitbit)
    "breath_rate",
    # кислород во сне и температура кожи — тоже Google Health
    "spo2_pct",
    "skin_temp_c",
    # Apple Watch: кольцо «Тренировка» и выносливость
    "exercise_min",
    "vo2max",
)


# Ранги источников, как в Apple Здоровье: выше ранг — приоритетнее.
# google = API Google Health (облако Fitbit), app = наше iOS-приложение
# (задел: пока данные из него не шлём), shortcuts = пуш «Команд».
SOURCE_RANK = {"shortcuts": 1, "app": 2, "google": 3, "manual": 4}

# кэш пользовательских настроек: приоритет и включённость источников
_user_sources: dict[int, dict[str, tuple[bool, int]]] = {}


async def source_settings(user_id: int) -> dict[str, tuple[bool, int]]:
    """{источник: (включён, ранг)} — из БД, с фолбэком на дефолт."""
    if user_id in _user_sources:
        return _user_sources[user_id]
    rows = await fetch(
        "SELECT source, enabled, rank FROM sync_sources WHERE user_id = %s",
        (user_id,))
    out = {src: (True, rank) for src, rank in SOURCE_RANK.items()}
    for r in rows:
        out[r["source"]] = (r["enabled"], r["rank"])
    _user_sources[user_id] = out
    return out


def drop_source_cache(user_id: int) -> None:
    _user_sources.pop(user_id, None)


async def log_sync(user_id: int, source: str, day, metrics: list[str]) -> None:
    """Журнал: кто и что прислал — для раздела «Синхронизация»."""
    if not metrics:
        return
    await execute(
        "INSERT INTO sync_log (user_id, source, day, metrics, count) "
        "VALUES (%s, %s, %s, %s, %s)",
        (user_id, source, day, ", ".join(metrics), len(metrics)))


async def upsert_health_day(user_id: int, day: date, values: dict[str, Any],
                            source: str = "shortcuts") -> None:
    """Апсертит день. Пустые поля не затирают сохранённые, а источник с
    меньшим рангом не затирает метрику, записанную источником выше
    (пуш «Команд» не перекроет сон, который пришёл из Google Health)."""
    cols = [f for f in HEALTH_FIELDS if values.get(f) is not None]
    if not cols:
        return
    settings = await source_settings(user_id)
    enabled, rank = settings.get(source, (True, SOURCE_RANK.get(source, 1)))
    if not enabled:
        return                      # источник выключен в настройках

    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT src FROM health_daily WHERE user_id = %s AND day = %s",
                (user_id, day))
            row = await cur.fetchone()
            src: dict = (row or {}).get("src") or {}
            cols = [c for c in cols
                    if rank >= (settings.get(src.get(c, ""), (True, 0))[1]
                                if src.get(c) else 0)]
            if not cols:
                return
            src.update({c: source for c in cols})

            placeholders = ", ".join(["%s"] * len(cols))
            updates = ", ".join(
                f"{c} = COALESCE(EXCLUDED.{c}, health_daily.{c})" for c in cols)
            await cur.execute(
                f"""
                INSERT INTO health_daily (user_id, day, src, {", ".join(cols)})
                VALUES (%s, %s, %s, {placeholders})
                ON CONFLICT (user_id, day) DO UPDATE SET {updates},
                    src = health_daily.src || EXCLUDED.src, updated_at = now()
                """,
                (user_id, day, Jsonb(src), *[values[c] for c in cols]),
            )
    await log_sync(user_id, source, day, cols)


# --------------------------------------------------------------------- цели

async def get_active_goals(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM goals WHERE user_id = %s AND is_active ORDER BY active_from DESC",
                (user_id,),
            )
            return await cur.fetchall()


async def set_goal(
    user_id: int,
    goal_text: str,
    kcal_target: int | None = None,
    protein_target: int | None = None,
) -> None:
    """Ставит цель, деактивируя предыдущую.

    Таргеты необязательны: цель формулируется словами («похудеть до 85 кг»),
    а норму калорий бот считает сам от веса и цели. Раньше их приходилось
    указывать в команде, и это мешало — человек не знает свою норму.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE goals SET is_active = FALSE WHERE user_id = %s", (user_id,)
        )
        await conn.execute(
            """
            INSERT INTO goals (user_id, goal_text, kcal_target, protein_target)
            VALUES (%s, %s, %s, %s)
            """,
            (user_id, goal_text, kcal_target, protein_target),
        )


# ------------------------------------------------------ произвольные выборки

async def execute(sql: str, params: tuple | dict[str, Any] = ()) -> None:
    """Запись без результата: INSERT/UPDATE вне готовых helper'ов."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(sql, params)


async def fetch(sql: str, params: tuple | dict[str, Any] = ()) -> list[dict[str, Any]]:
    """Read-only выборки: инструменты агента, сводки, самодиагностика.

    dict принимается для именованных плейсхолдеров %(name)s — удобно, когда
    один и тот же user_id подставляется в запрос много раз.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()


# ------------------------------------------------------- сводки и тренды

# Метрики, по которым бот умеет строить графики и тренды.
# ключ -> (колонка во v_daily_full, подпись, единица)
METRICS: dict[str, tuple[str, str, str]] = {
    "eaten": ("kcal_eaten", "Съеденные калории", "ккал"),
    "burned": ("kcal_burned", "Потраченные калории", "ккал"),
    "active": ("active_kcal", "Энергия активности", "ккал"),
    "resting": ("resting_kcal", "Энергия покоя", "ккал"),
    "balance": ("balance", "Баланс калорий", "ккал"),
    "sleep": ("sleep_hours", "Сон", "ч"),
    "steps": ("steps", "Шаги", ""),
    "distance": ("distance_km", "Дистанция", "км"),
    "weight": ("weight_kg", "Вес", "кг"),
    "fat": ("body_fat_pct", "Процент жира", "%"),
    "hrv": ("hrv_ms", "Вариабельность (HRV)", "мс"),
    "breath": ("breath_rate", "Частота дыхания во сне", "вдох/мин"),
    "spo2": ("spo2_pct", "Кислород во сне", "%"),
    "temp": ("skin_temp_c", "Температура кожи", "°C"),
    "exercise": ("exercise_min", "Минуты тренировок", "мин"),
    "vo2max": ("vo2max", "VO₂max", "мл/кг/мин"),
    "hr": ("resting_hr", "Пульс покоя", "уд/мин"),
    # ВСР и ИМТ приходят из «Быстрых команд» — раньше их не было вовсе
    "hrv": ("hrv_ms", "ВСР", "мс"),
    "bmi": ("bmi", "ИМТ", ""),
}


async def day_full(user_id: int, day: date) -> dict[str, Any] | None:
    """Все метрики за один день."""
    rows = await fetch(
        "SELECT * FROM v_daily_full WHERE user_id = %s AND day = %s", (user_id, day)
    )
    return rows[0] if rows else None


async def period_full(user_id: int, date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Все метрики за период, по возрастанию дат — для графиков и трендов."""
    return await fetch(
        """
        SELECT * FROM v_daily_full
        WHERE user_id = %s AND day BETWEEN %s AND %s
        ORDER BY day
        """,
        (user_id, date_from, date_to),
    )


async def averages(user_id: int, days: int, before: date | None = None) -> dict[str, Any] | None:
    """Средние за N дней до указанной даты. Нужны, чтобы считать тренд
    «эта неделя против прошлой»."""
    before = before or date.today()
    rows = await fetch(
        """
        SELECT avg(kcal_eaten) AS kcal_eaten, avg(kcal_burned) AS kcal_burned,
               avg(balance) AS balance, avg(sleep_hours) AS sleep_hours,
               avg(steps) AS steps, avg(distance_km) AS distance_km,
               avg(weight_kg) AS weight_kg, avg(body_fat_pct) AS body_fat_pct,
               avg(resting_hr) AS resting_hr, count(*) AS days
        FROM v_daily_full
        WHERE user_id = %s AND day > %s AND day <= %s
        """,
        (user_id, before - timedelta(days=days), before),
    )
    return rows[0] if rows else None


async def last_weight_day(user_id: int) -> date | None:
    """Когда последний раз был вес — для напоминания о взвешивании."""
    rows = await fetch(
        """
        SELECT max(day) AS day FROM health_daily
        WHERE user_id = %s AND weight_kg IS NOT NULL
        """,
        (user_id,),
    )
    return rows[0]["day"] if rows and rows[0]["day"] else None


async def known_users() -> list[int]:
    """Все, кто хоть что-то записал. Планировщик рассылает только им, чтобы
    не долбить сообщениями пустые аккаунты из ALLOWED_USER_IDS."""
    rows = await fetch(
        """
        SELECT DISTINCT user_id FROM (
            SELECT user_id FROM meals
            UNION SELECT user_id FROM health_daily
            UNION SELECT user_id FROM goals
        ) t
        """
    )
    return [r["user_id"] for r in rows]


# ------------------------------------------------------------------- память

# Окна по времени нет: разговор продолжается с того места, где остановились,
# даже если это было вчера. Ограничиваем только числом реплик.
DIALOG_MAX_MESSAGES = 5


async def save_message(user_id: int, role: str, text: str, intent: str | None = None) -> None:
    """Пишет реплику в историю. Ошибки глушим: сломанная память не должна
    ломать ответ пользователю."""
    pool = await get_pool()
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO messages (user_id, role, text, intent) VALUES (%s, %s, %s, %s)",
                (user_id, role, text[:4000], intent),
            )
    except Exception:
        log.exception("Не удалось сохранить сообщение в историю")


async def recent_dialog(user_id: int,
                        limit: int = DIALOG_MAX_MESSAGES) -> list[dict[str, Any]]:
    """Последние реплики диалога, по возрастанию времени — без окна по времени."""
    rows = await fetch(
        """
        SELECT role, text FROM (
            SELECT role, text, created_at
            FROM messages
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        ) t ORDER BY created_at
        """,
        (user_id, limit),
    )
    return rows


async def get_facts(user_id: int) -> list[str]:
    rows = await fetch(
        "SELECT fact FROM user_facts WHERE user_id = %s ORDER BY created_at", (user_id,)
    )
    return [r["fact"] for r in rows]


async def add_facts(user_id: int, facts: list[str], source: str = "chat") -> int:
    """Добавляет факты, пропуская дубли. Возвращает число новых."""
    if not facts:
        return 0
    pool = await get_pool()
    added = 0
    async with pool.connection() as conn:
        for fact in facts:
            fact = fact.strip()
            if not fact or len(fact) > 300:
                continue
            cur = await conn.execute(
                """
                INSERT INTO user_facts (user_id, fact, source) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, fact) DO NOTHING
                """,
                (user_id, fact, source),
            )
            added += cur.rowcount or 0
    if added:
        log.info("Запомнил %s новых фактов о %s", added, user_id)
    return added


async def cleanup_messages(days: int = 30) -> int:
    """Чистка истории: контекст старше месяца бесполезен, а таблица растёт."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM messages WHERE created_at < now() - make_interval(days => %s)",
            (days,),
        )
        return cur.rowcount or 0


# ------------------------------------------------------------------- анализы

async def save_labs(
    user_id: int,
    taken_on: date,
    items: list[dict[str, Any]],
    lab: str | None = None,
    source: str | None = None,
    raw_input: str | None = None,
) -> int:
    """Сохраняет показатели одной сдачи. Возвращает число записанных.

    Флаг «вне нормы» считаем здесь: границы у лабораторий разные, и сравнивать
    в каждом запросе неудобно. Повторная сдача того же показателя в тот же день
    перезаписывается — обычно это уточнение, а не второй анализ.
    """
    if not items:
        return 0

    pool = await get_pool()
    saved = 0
    async with pool.connection() as conn:
        for it in items:
            value = it.get("value")
            low, high = it.get("ref_low"), it.get("ref_high")
            flag = None
            if value is not None:
                if low is not None and float(value) < float(low):
                    flag = "low"
                elif high is not None and float(value) > float(high):
                    flag = "high"
                elif low is not None or high is not None:
                    flag = "normal"

            await conn.execute(
                """
                INSERT INTO lab_results
                    (user_id, taken_on, name, canonical, value, unit,
                     ref_low, ref_high, flag, lab, source, raw_input)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, taken_on, name) DO UPDATE SET
                    value = EXCLUDED.value,
                    unit = EXCLUDED.unit,
                    ref_low = EXCLUDED.ref_low,
                    ref_high = EXCLUDED.ref_high,
                    flag = EXCLUDED.flag,
                    lab = COALESCE(EXCLUDED.lab, lab_results.lab)
                """,
                (
                    user_id, taken_on, it.get("name") or "?",
                    it.get("canonical"), value, it.get("unit"),
                    low, high, flag, lab, source, raw_input,
                ),
            )
            saved += 1
    log.info("Записано показателей анализов: %s за %s", saved, taken_on)
    return saved


async def last_labs(user_id: int) -> list[dict[str, Any]]:
    """Показатели самой свежей сдачи."""
    return await fetch(
        """
        SELECT id, name, canonical, value, unit, ref_low, ref_high, flag, taken_on, lab
        FROM lab_results
        WHERE user_id = %s
          AND taken_on = (SELECT max(taken_on) FROM lab_results WHERE user_id = %s)
        ORDER BY (flag = 'normal'), name
        """,
        (user_id, user_id),
    )


async def lab_dates(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    """Список сдач: дата, сколько показателей, сколько вне нормы."""
    return await fetch(
        """
        SELECT taken_on, count(*) AS total,
               count(*) FILTER (WHERE flag IN ('low', 'high')) AS off_norm
        FROM lab_results WHERE user_id = %s
        GROUP BY taken_on ORDER BY taken_on DESC LIMIT %s
        """,
        (user_id, limit),
    )


# ------------------------------------------------------------------ профиль

async def get_user(user_id: int) -> dict[str, Any] | None:
    """Профиль или None, если человек ещё не регистрировался."""
    rows = await fetch(
        """
        SELECT user_id, name, sex, birth_date, height_cm, tz, registered_at,
               date_part('year', age(birth_date))::int AS age
        FROM users WHERE user_id = %s
        """,
        (user_id,),
    )
    return rows[0] if rows else None


async def is_registered(user_id: int) -> bool:
    rows = await fetch("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
    return bool(rows)


async def upsert_user(user_id: int, name: str, sex: str | None,
                      birth_date: Any, height_cm: float | None) -> None:
    """Создаёт профиль или обновляет существующий.

    ON CONFLICT, а не INSERT: форму можно открыть второй раз, чтобы поправить
    рост или имя — падать на этом незачем.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, name, sex, birth_date, height_cm)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                name = EXCLUDED.name,
                sex = EXCLUDED.sex,
                birth_date = EXCLUDED.birth_date,
                height_cm = EXCLUDED.height_cm,
                updated_at = now()
            """,
            (user_id, name, sex, birth_date, height_cm),
        )


# --------------------------------------------------- черновики записей о еде

async def create_draft(draft_id: str, user_id: int, items: list[dict[str, Any]],
                       source: str, raw_input: str | None = None,
                       photo_file_id: str | None = None,
                       meal_id: int | None = None) -> None:
    """Кладёт распознанный список в базу под коротким id.

    Через базу, а не через память процесса: мини-апп стучится в API, а бот и
    API — разные контейнеры, общего словаря у них нет.
    """
    import json

    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            """
            INSERT INTO meal_drafts
                (id, user_id, items, source, raw_input, photo_file_id, meal_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET items = EXCLUDED.items
            """,
            (draft_id, user_id, json.dumps(items, ensure_ascii=False, default=str),
             source, raw_input, photo_file_id, meal_id),
        )


async def get_draft(draft_id: str, user_id: int) -> dict[str, Any] | None:
    """Черновик по id. user_id в условии — чтобы по чужой ссылке не открылось."""
    rows = await fetch(
        """
        SELECT id, user_id, items, source, raw_input, photo_file_id, meal_id
        FROM meal_drafts WHERE id = %s AND user_id = %s
        """,
        (draft_id, user_id),
    )
    return rows[0] if rows else None


async def delete_draft(draft_id: str) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM meal_drafts WHERE id = %s", (draft_id,))


async def cleanup_drafts(hours: int = 24) -> int:
    """Чистит брошенные черновики: человек открыл редактор и не сохранил."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "DELETE FROM meal_drafts WHERE created_at < now() - make_interval(hours => %s)",
            (hours,),
        )
        return cur.rowcount or 0


async def meal_items_for_edit(meal_id: int, user_id: int) -> list[dict[str, Any]]:
    """Позиции записи для редактора в мини-аппе.

    kcal_100 и остальные «на 100 г» пересчитываем из сохранённых значений:
    в meal_items лежит только итог на порцию, а редактору нужна калорийность
    продукта — по ней он пересчитывает калории при смене граммовки.

    user_id в условии, чтобы по чужому meal_id ничего не открылось.
    """
    return await fetch(
        """
        SELECT i.name, i.resolved_name, i.grams, i.kcal, i.protein, i.fat, i.carbs,
               i.confidence, i.food_source,
               CASE WHEN i.grams > 0 THEN round(i.kcal * 100 / i.grams, 1) END AS kcal_100,
               CASE WHEN i.grams > 0 THEN round(i.protein * 100 / i.grams, 1) END AS protein_100,
               CASE WHEN i.grams > 0 THEN round(i.fat * 100 / i.grams, 1) END AS fat_100,
               CASE WHEN i.grams > 0 THEN round(i.carbs * 100 / i.grams, 1) END AS carbs_100
        FROM meal_items i
        JOIN meals m ON m.id = i.meal_id
        WHERE i.meal_id = %s AND m.user_id = %s
        ORDER BY i.id
        """,
        (meal_id, user_id),
    )


async def suggest_foods(user_id: int, query: str, limit: int = 6) -> list[dict[str, Any]]:
    """Продукты из кеша по началу названия — для подсказок в редакторе.

    Ищем и по alias (как человек это называет), и по canonical_name (как
    записалось в справочнике): вбивая «греч», человек ждёт «Гречка отварная»,
    хотя alias у неё «гречка с курицей».

    Сортируем по hits: то, что ешь часто, должно быть первым.
    """
    like = f"%{query.strip().lower()}%"
    return await fetch(
        """
        SELECT canonical_name, kcal_100, protein_100, fat_100, carbs_100,
               default_grams, hits
        FROM my_foods
        WHERE user_id = %s AND (alias ILIKE %s OR canonical_name ILIKE %s)
        ORDER BY hits DESC, updated_at DESC
        LIMIT %s
        """,
        (user_id, like, like, limit),
    )


async def meals_by_day(user_id: int, day: Any) -> list[dict[str, Any]]:
    """Приёмы пищи за конкретный день вместе с позициями.

    Позиции отдаём вложенным JSON, а не отдельным запросом на каждый приём:
    редактору нужны сразу все, а N+1 запросов на день с шестью приёмами —
    лишние круги к базе.

    kcal_100 и остальные «на 100 г» считаем из сохранённого итога: в meal_items
    лежат значения на порцию, а редактор пересчитывает калории от 100 г.
    """
    return await fetch(
        """
        SELECT m.id,
               (m.eaten_at AT TIME ZONE 'Europe/Moscow') AS eaten_at,
               m.source,
               ROUND(SUM(i.kcal)) AS kcal,
               jsonb_agg(
                   jsonb_build_object(
                       'name', i.name,
                       'resolved_name', COALESCE(i.resolved_name, i.name),
                       'grams', i.grams,
                       'kcal', i.kcal,
                       'protein', i.protein,
                       'fat', i.fat,
                       'carbs', i.carbs,
                       'food_source', i.food_source,
                       'kcal_100', CASE WHEN i.grams > 0
                           THEN ROUND(i.kcal * 100 / i.grams, 1) END,
                       'protein_100', CASE WHEN i.grams > 0
                           THEN ROUND(i.protein * 100 / i.grams, 1) END,
                       'fat_100', CASE WHEN i.grams > 0
                           THEN ROUND(i.fat * 100 / i.grams, 1) END,
                       'carbs_100', CASE WHEN i.grams > 0
                           THEN ROUND(i.carbs * 100 / i.grams, 1) END
                   ) ORDER BY i.id
               ) AS items
        FROM meals m
        JOIN meal_items i ON i.meal_id = m.id
        WHERE m.user_id = %s
          AND (m.eaten_at AT TIME ZONE 'Europe/Moscow')::date = %s
        GROUP BY m.id, m.eaten_at, m.source
        ORDER BY m.eaten_at
        """,
        (user_id, day),
    )


async def days_with_meals(user_id: int, limit: int = 30) -> list[Any]:
    """Дни, за которые есть записи — для навигации по датам в редакторе.

    Нужны, чтобы не листать пустые дни: показываем только те, где что-то есть.
    """
    rows = await fetch(
        """
        SELECT DISTINCT (eaten_at AT TIME ZONE 'Europe/Moscow')::date AS day
        FROM meals WHERE user_id = %s
        ORDER BY day DESC LIMIT %s
        """,
        (user_id, limit),
    )
    return [r["day"] for r in rows]


async def update_meal_items(meal_id: int, user_id: int,
                            items: list[dict[str, Any]]) -> bool:
    """Заменяет позиции записи, сохраняя саму запись.

    Через delete+insert, а не UPDATE по одной: позиции могли добавиться или
    исчезнуть, а сопоставлять их по порядку — источник тихих ошибок. id самого
    meal при этом сохраняется, время приёма тоже.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM meals WHERE id = %s AND user_id = %s",
                (meal_id, user_id),
            )
            if not await cur.fetchone():
                return False

            await cur.execute("DELETE FROM meal_items WHERE meal_id = %s", (meal_id,))
            for it in items:
                await cur.execute(
                    """
                    INSERT INTO meal_items
                        (meal_id, name, resolved_name, grams, kcal, protein, fat,
                         carbs, confidence, food_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (meal_id, it["name"], it.get("resolved_name"), it["grams"],
                     it["kcal"], it.get("protein", 0), it.get("fat", 0),
                     it.get("carbs", 0), it.get("confidence"),
                     it.get("food_source")),
                )
    return True
