"""Проактивные сообщения: утренняя сводка, напоминания, алерты.

Единственное место, где бот пишет первым. Всё остальное в проекте отвечает
на входящие сообщения.

Шесть задач:
  digest   — каждое утро: вчера + тренд за неделю + рекомендации
  evening  — каждый вечер: итоги дня + что поправить завтра
  weigh    — если N дней не было веса, напомнить взвеситься
  insight  — раз в неделю разбор динамики за 14 дней
  alert    — вес идёт против цели: предупредить
  missing  — за вчера не пришли данные из Apple Health: предупредить

Почему APScheduler, а не cron в контейнере: задачам нужен тот же пул Postgres
и та же сессия бота, что и хендлерам. Отдельный процесс тянул бы вторую копию
конфига и соединений.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import prompts
from app.config import get_settings
from app.db import repo
from app.providers.factory import get_llm

log = logging.getLogger(__name__)

# Насколько вес должен уйти против цели, чтобы бот забил тревогу.
# Меньше килограмма — это вода и время взвешивания, а не тренд.
ALERT_WEIGHT_DELTA_KG = 1.0
ALERT_WINDOW_DAYS = 14


# ----------------------------------------------------------------- форматирование

def _fmt_metrics(row: dict[str, Any] | None) -> str:
    """Строки «Метрика: значение» только по тем метрикам, что реально есть.
    Пустые не показываем — модель иначе начинает придумывать по ним выводы."""
    if not row:
        return "нет данных"
    out = []
    for key, (col, label, unit) in repo.METRICS.items():
        value = row.get(col)
        if value is None:
            continue
        value = float(value)
        shown = f"{value:.1f}".rstrip("0").rstrip(".") if unit != "" else f"{value:.0f}"
        out.append(f"- {label}: {shown} {unit}".rstrip())
    return "\n".join(out) if out else "нет данных"


# Иконка над колонкой в таблице-картинке. Ключи — из repo.METRICS,
# имена иконок — из services/tables.py EMOJI.
_METRIC_ICONS = {
    "burned": "fire", "active": "fire", "resting": "fire",
    "eaten": "plate", "balance": "scales",
    "weight": "weight", "fat": "weight",
    "sleep": "bed", "steps": "steps", "distance": "steps",
    "hr": "heart", "hrv": "heart",
}


def _metrics_table(row: dict[str, Any] | None, title: str,
                   footer: str = "") -> dict | None:
    """Данные для картинки с метриками дня — для утренней и вечерней сводок.

    Раньше метрики шли текстом «- Сон: 7.2 ч» списком на десять строк: в чате
    это выглядело простынёй, а цифры терялись. Картинка кладёт их в таблицу,
    как в разделах бота.

    None — если данных нет: пустую таблицу рисовать незачем.

    Метрика в СТРОКУ, а не в столбец: их до одиннадцати, и в одну строку
    картинка выходила 2600 px шириной — на телефоне подписи нечитаемы.
    """
    if not row:
        return None

    rows_out, icons = [], []
    for key, (col, label, unit) in repo.METRICS.items():
        value = row.get(col)
        if value is None:
            continue
        value = float(value)
        shown = f"{value:.1f}".rstrip("0").rstrip(".") if unit else f"{value:.0f}"
        rows_out.append([label, f"{shown} {unit}".strip()])
        icons.append(_METRIC_ICONS.get(key))

    if not rows_out:
        return None

    return {
        "title": title,
        # без шапки: колонки «что» и «сколько» подписывать незачем
        "header": ["", ""],
        "rows": rows_out,
        "aligns": ["l", "r"],
        "icons": icons,
        "footer": footer,
    }


async def _send_table_photo(bot: Bot, user_id: int, data: dict) -> None:
    """Отправляет таблицу-картинку из планировщика.

    Отдельно от services.tables.send_table: та работает с Message (отвечает на
    сообщение), а здесь есть только Bot и user_id — сводка приходит сама, без
    входящего сообщения.

    Рендер в поток: matplotlib блокирующий, а планировщик крутится в том же
    event loop, что и polling.
    """
    import asyncio

    from aiogram.types import BufferedInputFile

    from app.services.tables import render_table

    try:
        png = await asyncio.to_thread(
            render_table, data["title"], data["header"], data["rows"],
            data.get("aligns"), data.get("colors"), data.get("footer", ""),
            data.get("icons"), data.get("head_icons"),
        )
        await bot.send_photo(user_id, BufferedInputFile(png, filename="day.png"))
    except Exception:
        # картинка не главное: рекомендация придёт следующим сообщением
        log.exception("Не удалось отправить таблицу сводки %s", user_id)


def _trend_arrow(now: float | None, was: float | None, unit: str) -> str:
    if now is None or was is None:
        return ""
    delta = float(now) - float(was)
    if abs(delta) < 0.05:
        return " (без изменений)"
    sign = "▲" if delta > 0 else "▼"
    return f" ({sign} {abs(delta):.1f} {unit})".replace(" )", ")")


async def _goal_text(user_id: int) -> str:
    goals = await repo.get_active_goals(user_id)
    return goals[0]["goal_text"] if goals else "не задана"


async def _llm_text(system: str, user: str, fallback: str) -> str:
    """Обёртка: сообщение планировщика не должно падать из-за недоступной модели."""
    try:
        return (await get_llm().complete(system, user)).strip() or fallback
    except Exception:
        log.exception("LLM недоступна в планировщике")
        return fallback


# ----------------------------------------------------------------- задачи

async def _today() -> date:
    """Сегодня по Москве, а не по UTC контейнера.

    Планировщик срабатывает в 10:00 и 22:00 МСК. В UTC это 07:00 и 19:00 того
    же дня, но date.today() в контейнере до 03:00 МСК показывает вчера —
    вечерняя сводка брала не тот день.
    """
    try:
        rows = await repo.fetch(
            "SELECT (now() AT TIME ZONE 'Europe/Moscow')::date AS d"
        )
        return rows[0]["d"]
    except Exception:
        log.exception("Не удалось взять дату из базы")
        return date.today()


async def send_digest(bot: Bot, user_id: int) -> None:
    """Утренняя сводка: вчера, тренд неделя к неделе, рекомендации."""
    yesterday = await _today() - timedelta(days=1)
    day = await repo.day_full(user_id, yesterday)
    week = await repo.averages(user_id, 7)
    prev = await repo.averages(user_id, 7, before=await _today() - timedelta(days=7))

    if not day and not (week and week.get("days")):
        log.info("Сводка для %s пропущена: данных нет", user_id)
        return

    # тренд считаем сами, а не просим модель: арифметику она врёт чаще, чем текст
    trend = ""
    if week and prev and week.get("days") and prev.get("days"):
        parts = []
        # полный набор: потрачено, покой, активность, съедено, сон, дистанция,
        # вес, жир, пульс — как в требованиях к утренней сводке
        for key in ("burned", "resting", "active", "eaten", "sleep",
                    "distance", "weight", "fat", "hr"):
            col, label, unit = repo.METRICS[key]
            arrow = _trend_arrow(week.get(col), prev.get(col), unit)
            if arrow:
                parts.append(f"{label}{arrow}")
        if parts:
            trend = "неделя к неделе: " + " · ".join(parts)

    # Метрики картинкой: тренд уходит в подпись под таблицей, чтобы цифры и
    # их динамика читались вместе, а не двумя отдельными блоками текста.
    table = _metrics_table(day, f"Итоги дня, {yesterday:%d.%m}", trend)
    if table:
        await _send_table_photo(bot, user_id, table)

    comment = await _llm_text(
        prompts.DIGEST_SYSTEM,
        prompts.DIGEST_USER.format(
            day=f"{yesterday:%d.%m}",
            yesterday=_fmt_metrics(day),
            week=_fmt_metrics(week),
            prev_week=_fmt_metrics(prev),
            goal=await _goal_text(user_id),
        ),
        fallback="",
    )
    # рекомендация текстом под картинкой
    if comment:
        await bot.send_message(user_id, comment, parse_mode="Markdown")
    elif not table:
        # ни таблицы, ни совета — говорим прямо, иначе сводка молча пропадает
        await bot.send_message(
            user_id, f"*Итоги дня, {yesterday:%d.%m}*\nданных за день нет",
            parse_mode="Markdown")


async def send_evening_digest(bot: Bot, user_id: int) -> None:
    """Вечерняя сводка: как прошёл день и что поправить завтра.

    Берём сегодняшний день, а не вчерашний: смысл именно в том, чтобы успеть
    среагировать — например, добрать белок или лечь раньше.
    """
    today = await _today()
    day = await repo.day_full(user_id, today)
    week = await repo.averages(user_id, 7)

    if not day:
        log.info("Вечерняя сводка для %s пропущена: за сегодня данных нет", user_id)
        return

    table = _metrics_table(day, f"Как прошёл день, {today:%d.%m}")
    if table:
        await _send_table_photo(bot, user_id, table)

    comment = await _llm_text(
        prompts.EVENING_SYSTEM,
        prompts.EVENING_USER.format(
            day=f"{today:%d.%m}",
            today=_fmt_metrics(day),
            week=_fmt_metrics(week),
            goal=await _goal_text(user_id),
        ),
        fallback="",
    )
    if comment:
        await bot.send_message(user_id, comment, parse_mode="Markdown")


# Метрики, отсутствие которых за вчера означает поломку выгрузки.
# Это КЛЮЧИ repo.METRICS, а не имена колонок: у активных калорий ключ "active",
# а колонка "active_kcal".
# Вес и процент жира сюда не входят: человек может просто не встать на весы.
EXPECTED_YESTERDAY = ("steps", "active", "sleep")


async def check_missing_data(bot: Bot, user_id: int) -> None:
    """Алерт, если за вчера не пришли данные из Apple Health.

    Автоматизация на телефоне отваливается молча — без этой проверки пропажу
    замечаешь через неделю по пустым графикам.
    """
    yesterday = await _today() - timedelta(days=1)
    day = await repo.day_full(user_id, yesterday)

    if day is None:
        await bot.send_message(
            user_id,
            f"⚠️ За {yesterday:%d.%m} из Apple Health не пришло ничего.\n"
            "Проверь автоматизацию на телефоне: «Быстрые команды» или Health Auto Export.",
        )
        return

    missing = [
        label
        for key, (col, label, _) in repo.METRICS.items()
        if key in EXPECTED_YESTERDAY and day.get(col) is None
    ]
    if missing:
        await bot.send_message(
            user_id,
            f"⚠️ За {yesterday:%d.%m} не пришло: {', '.join(missing)}.\n"
            "Остальное на месте — похоже, отвалилась часть выгрузки.",
        )


async def run_quality_eval() -> None:
    """Регрессия качества: три вопроса, метрики уходят в Langfuse.

    Берём одного пользователя — эталонного: метрики нужны про модель,
    а не про каждого человека, и лишние прогоны стоили бы токенов.
    """
    from app.services import eval as quality

    rows = await repo.fetch(
        "SELECT user_id FROM users ORDER BY user_id LIMIT 1")
    if not rows:
        log.info("Оценка качества: пользователей нет, пропускаю")
        return
    try:
        await quality.run(rows[0]["user_id"])
    except Exception:
        log.exception("Оценка качества упала")


async def cleanup_old_messages(bot: Bot, user_id: int) -> None:
    """Чистка истории диалога. Контекст старше месяца бесполезен, а таблица
    растёт с каждым сообщением. Факты о пользователе не трогаем — они и нужны
    как долгая память."""
    from app.db import repo

    deleted = await repo.cleanup_messages(days=30)
    if deleted:
        log.info("Удалено старых сообщений: %s", deleted)

    # Заодно черновики редактора: их заводит каждая кнопка «Править» в /last,
    # и брошенные накапливаются, даже если человек ничего не менял.
    drafts = await repo.cleanup_drafts(hours=24)
    if drafts:
        log.info("Удалено брошенных черновиков: %s", drafts)


# Насколько пульс покоя должен уйти выше личной нормы, чтобы это считалось
# сигналом. 5 ударов — примерно то, что дают простуда или тяжёлая тренировка;
# ниже этого порога шум перекрывает сигнал.
STRESS_HR_DELTA = 5
# ВСР ниже 80% от личной нормы — общепринятый признак недовосстановления
STRESS_HRV_RATIO = 0.8
STRESS_SLEEP_HOURS = 5.0
# не чаще одного алерта в двое суток, иначе при затяжном недосыпе он придёт
# каждый день и перестанет читаться
STRESS_COOLDOWN_HOURS = 48


async def check_stress(bot: Bot, user_id: int) -> None:
    """Алерт при признаках напряжения в свежих данных.

    Сравниваем с личной нормой, а не с абсолютным порогом: пульс покоя 54 и 70
    у разных людей одинаково нормальны, важно отклонение от своего обычного.
    """
    today = await _today()

    baseline = await repo.fetch(
        """
        SELECT round(avg(resting_hr), 1) AS hr_avg,
               round(avg(hrv_ms), 1) AS hrv_avg,
               round(avg(sleep_hours), 1) AS sleep_avg,
               count(resting_hr) AS hr_days
        FROM health_daily
        WHERE user_id = %s AND day < %s
        """,
        (user_id, today - timedelta(days=2)),
    )
    base = baseline[0] if baseline else {}
    # без истории сравнивать не с чем: на трёх днях «норма» будет случайной
    if not base or (base.get("hr_days") or 0) < 7:
        return

    recent = await repo.fetch(
        """
        SELECT day, resting_hr, hrv_ms, sleep_hours, steps,
               round(kcal_eaten) AS eaten, round(kcal_burned) AS burned
        FROM v_daily_full
        WHERE user_id = %s AND day >= %s
        ORDER BY day DESC
        """,
        (user_id, today - timedelta(days=3)),
    )
    if not recent:
        return

    triggers = []
    for r in recent:
        hr, hrv, sleep = r.get("resting_hr"), r.get("hrv_ms"), r.get("sleep_hours")
        if hr is not None and base.get("hr_avg"):
            delta = float(hr) - float(base["hr_avg"])
            if delta >= STRESS_HR_DELTA:
                triggers.append(
                    f"пульс покоя {hr} при норме {base['hr_avg']} "
                    f"(+{delta:.0f}) — {r['day']:%d.%m}"
                )
        if hrv is not None and base.get("hrv_avg"):
            if float(hrv) < float(base["hrv_avg"]) * STRESS_HRV_RATIO:
                triggers.append(
                    f"ВСР {hrv} мс при норме {base['hrv_avg']} — {r['day']:%d.%m}"
                )
        if sleep is not None and float(sleep) < STRESS_SLEEP_HOURS:
            triggers.append(f"сон {float(sleep):.1f} ч — {r['day']:%d.%m}")

    if not triggers:
        return

    # антиспам: смотрим, когда последний раз писали про стресс
    sent = await repo.fetch(
        """
        SELECT max(created_at) AS last FROM messages
        WHERE user_id = %s AND role = 'assistant' AND intent = 'stress_alert'
        """,
        (user_id,),
    )
    last = sent[0]["last"] if sent else None
    if last is not None:
        from datetime import datetime, timezone

        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if hours < STRESS_COOLDOWN_HOURS:
            log.info("Алерт по стрессу пропущен: последний %.0f ч назад", hours)
            return

    def fmt(rows: list[dict]) -> str:
        out = []
        for r in rows:
            vals = [f"{k}={v}" for k, v in r.items() if k != "day" and v is not None]
            if vals:
                out.append(f"{r['day']:%d.%m} " + " ".join(vals))
        return "\n".join(out) or "нет данных"

    base_text = " · ".join(
        f"{label} {base[key]}"
        for key, label in (("hr_avg", "пульс покоя"), ("hrv_avg", "ВСР"),
                           ("sleep_avg", "сон"))
        if base.get(key) is not None
    )

    text = await _llm_text(
        prompts.STRESS_ALERT_SYSTEM,
        prompts.STRESS_ALERT_USER.format(
            trigger="; ".join(triggers[:4]),
            recent=fmt(recent),
            baseline=base_text or "нет данных",
            goal=await _goal_text(user_id),
        ),
        fallback="",
    )
    if not text:
        return

    await bot.send_message(user_id, f"🫂 {text}")
    # помечаем intent, чтобы работал антиспам и алерт попал в контекст диалога
    from app.db import repo as _repo

    await _repo.save_message(user_id, "assistant", text, intent="stress_alert")
    log.info("Отправлен алерт по стрессу: %s", triggers[0])


async def send_weigh_reminder(bot: Bot, user_id: int) -> None:
    """Напоминание взвеситься, если веса давно нет."""
    limit = get_settings().weigh_reminder_days
    last = await repo.last_weight_day(user_id)
    today = await _today()

    if last is None:
        # веса не было никогда — напомним только если человек вообще пользуется ботом
        days = None
    else:
        days = (today - last).days
        if days < limit:
            return

    if days is None:
        text = (
            "Веса в базе пока нет совсем. Взвесься — без него тренд по цели "
            "не построить, а это самая честная метрика."
        )
    else:
        text = (
            f"Последнее взвешивание было {last:%d.%m}, {days} дней назад. "
            "Встань на весы утром до завтрака — тренд поедет точнее."
        )
    await bot.send_message(user_id, text)


async def send_insight(bot: Bot, user_id: int) -> None:
    """Недельный разбор динамики за 14 дней."""
    rows = await repo.period_full(user_id, date.today() - timedelta(days=14), date.today())
    filled = [r for r in rows if r.get("kcal_eaten") or r.get("weight_kg")]
    if len(filled) < 4:
        log.info("Разбор для %s пропущен: всего %s дней с данными", user_id, len(filled))
        return

    table = "\n".join(
        f"{r['day']:%d.%m}: "
        + ", ".join(
            f"{label} {float(r[col]):.1f}{unit}"
            for _, (col, label, unit) in repo.METRICS.items()
            if r.get(col) is not None
        )
        for r in rows
    )
    text = await _llm_text(
        prompts.INSIGHT_SYSTEM,
        prompts.INSIGHT_USER.format(rows=table, goal=await _goal_text(user_id)),
        fallback="",
    )
    if text:
        await bot.send_message(user_id, f"*Разбор за две недели*\n\n{text}", parse_mode="Markdown")


async def check_weight_alert(bot: Bot, user_id: int) -> None:
    """Вес идёт против цели — предупредить.

    Направление берём из текста цели: «минус»/«похуд» -> вниз, «набрать» -> вверх.
    Без явного направления не алертим: молчание лучше ложной тревоги.
    """
    goals = await repo.get_active_goals(user_id)
    if not goals:
        return
    goal = goals[0]["goal_text"].lower()

    if any(w in goal for w in ("минус", "похуд", "сброс", "снизить")):
        want = "down"
    elif any(w in goal for w in ("набрать", "плюс", "масс", "набор")):
        want = "up"
    else:
        return

    rows = await repo.period_full(
        user_id, date.today() - timedelta(days=ALERT_WINDOW_DAYS), date.today()
    )
    weights = [(r["day"], float(r["weight_kg"])) for r in rows if r.get("weight_kg") is not None]
    if len(weights) < 2:
        return

    (day_from, w_from), (day_to, w_to) = weights[0], weights[-1]
    delta = w_to - w_from
    wrong_way = (want == "down" and delta >= ALERT_WEIGHT_DELTA_KG) or (
        want == "up" and delta <= -ALERT_WEIGHT_DELTA_KG
    )
    if not wrong_way:
        return

    avg = await repo.averages(user_id, ALERT_WINDOW_DAYS)
    text = await _llm_text(
        prompts.ALERT_SYSTEM,
        prompts.ALERT_USER.format(
            goal=goals[0]["goal_text"],
            weight_from=f"{w_from:.1f}",
            weight_to=f"{w_to:.1f}",
            days_ago=(day_to - day_from).days,
            balance=f"{float(avg['balance']):.0f}" if avg and avg.get("balance") else "нет данных",
            sleep=f"{float(avg['sleep_hours']):.1f}" if avg and avg.get("sleep_hours") else "нет данных",
        ),
        fallback=(
            f"Вес идёт против цели: {w_from:.1f} → {w_to:.1f} кг "
            f"за {(day_to - day_from).days} дней."
        ),
    )
    await bot.send_message(user_id, f"⚠️ *Отклонение от плана*\n\n{text}", parse_mode="Markdown")


# ----------------------------------------------------------------- обвязка

async def _for_each_user(bot: Bot, task, name: str) -> None:
    """Одна задача на всех известных пользователей.

    Ошибка у одного не должна ронять рассылку остальным и тем более планировщик:
    APScheduler при исключении просто пишет в лог, но следующий запуск может
    прийти только через сутки.
    """
    allowed = set(get_settings().allowed_user_ids)
    users = [u for u in await repo.known_users() if not allowed or u in allowed]
    for user_id in users:
        try:
            await task(bot, user_id)
        except Exception:
            log.exception("Задача %s упала для %s", name, user_id)


def setup_scheduler(bot: Bot) -> AsyncIOScheduler | None:
    """Регистрирует задачи. Возвращает None, если планировщик выключен."""
    s = get_settings()
    if not s.scheduler_enabled:
        log.info("Планировщик выключен (SCHEDULER_ENABLED=false)")
        return None

    sched = AsyncIOScheduler(timezone=s.tz)

    def shifted(minutes: int) -> tuple[int, int]:
        """Время сводки + N минут, с переносом через час: при DIGEST_MINUTE=55
        наивное +5 дало бы minute=60, и CronTrigger упал бы на старте."""
        total = (s.digest_hour * 60 + s.digest_minute + minutes) % (24 * 60)
        return total // 60, total % 60

    sched.add_job(
        _for_each_user, CronTrigger(hour=s.digest_hour, minute=s.digest_minute),
        args=(bot, send_digest, "digest"), id="digest", name="утренняя сводка",
    )
    # напоминание о весе — в то же утро, но позже сводки, чтобы не слиплось
    weigh_h, weigh_m = shifted(5)
    sched.add_job(
        _for_each_user, CronTrigger(hour=weigh_h, minute=weigh_m),
        args=(bot, send_weigh_reminder, "weigh"), id="weigh", name="напоминание о весе",
    )
    insight_h, insight_m = shifted(10)
    sched.add_job(
        _for_each_user,
        CronTrigger(day_of_week=s.insight_weekday, hour=insight_h, minute=insight_m),
        args=(bot, send_insight, "insight"), id="insight", name="недельный разбор",
    )
    # алерт по весу — дважды в день, чтобы не ждать сутки после плохого взвешивания
    sched.add_job(
        _for_each_user, CronTrigger(hour=f"{s.digest_hour},21", minute=30),
        args=(bot, check_weight_alert, "alert"), id="alert", name="алерт по весу",
    )
    # вечерняя сводка — пока день ещё можно поправить
    sched.add_job(
        _for_each_user, CronTrigger(hour=s.evening_hour, minute=s.evening_minute),
        args=(bot, send_evening_digest, "evening"), id="evening", name="вечерняя сводка",
    )
    # проверка стресса дважды в день: утром после ночной выгрузки Health
    # и вечером, когда набрались данные за день
    for hour in (11, 20):
        sched.add_job(
            _for_each_user, CronTrigger(hour=hour, minute=30),
            args=(bot, check_stress, "stress"), id=f"stress{hour}",
            name=f"проверка стресса {hour}:30",
        )
    # сон и пульс из облака Fitbit — каждый час, если кто-то подключён
    from app.services import fitbit
    # 13 запросов за проход (все метрики): раз в 6 минут = 130/час,
    # под квотой (~150/час на пользователя)
    sched.add_job(
        fitbit.sync_all, CronTrigger(minute="*/6"),
        id="fitbit", name="синхронизация Fitbit",
    )
    # чистка истории диалога — раз в сутки, ночью
    sched.add_job(
        _for_each_user, CronTrigger(hour=4, minute=0),
        args=(bot, cleanup_old_messages, "cleanup"), id="cleanup", name="чистка истории",
    )
    # LLM-судья: 1-го и 15-го числа в 5 утра. Раз в две недели и всего
    # 3 примера — прогон стоит примерно как одно сообщение пользователю.
    sched.add_job(
        run_quality_eval, CronTrigger(day="1,15", hour=5, minute=0),
        id="quality", name="оценка качества (LLM-судья)",
    )
    # пропажа данных — сразу после утренней сводки: к этому времени ночная
    # выгрузка за вчера уже должна была дойти
    missing_h, missing_m = shifted(15)
    sched.add_job(
        _for_each_user, CronTrigger(hour=missing_h, minute=missing_m),
        args=(bot, check_missing_data, "missing"), id="missing", name="алерт о пропаже данных",
    )

    sched.start()
    log.info(
        "Планировщик запущен (%s): утро %02d:%02d, вечер %02d:%02d, "
        "напоминание о весе через %s дней, задач %s",
        s.tz, s.digest_hour, s.digest_minute, s.evening_hour, s.evening_minute,
        s.weigh_reminder_days, len(sched.get_jobs()),
    )
    return sched


# ----------------------------------------------------------------- ручной запуск

async def _main() -> None:
    """Запуск задачи вручную, не дожидаясь расписания:

        docker compose exec bot python -m app.scheduler digest
        docker compose exec bot python -m app.scheduler weigh|insight|alert
    """
    import argparse
    import asyncio

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from app.db.pool import close_pool

    tasks = {
        "digest": send_digest,
        "weigh": send_weigh_reminder,
        "insight": send_insight,
        "alert": check_weight_alert,
        "evening": send_evening_digest,
        "missing": check_missing_data,
        "cleanup": cleanup_old_messages,
        "stress": check_stress,
        # без пользователя: судья сам берёт эталонного
        "quality": lambda bot, uid=None: run_quality_eval(),
    }
    ap = argparse.ArgumentParser(description="Ручной запуск задач планировщика")
    ap.add_argument("task", choices=sorted(tasks))
    ap.add_argument("--user", type=int, help="кому (по умолчанию всем известным)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    s = get_settings()
    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    try:
        if args.user:
            await tasks[args.task](bot, args.user)
        else:
            await _for_each_user(bot, tasks[args.task], args.task)
        print(f"Задача {args.task} выполнена")
    finally:
        await close_pool()
        await bot.session.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
