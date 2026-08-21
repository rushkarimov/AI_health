"""Команды бота."""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from contextlib import suppress
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app import prompts
from app.db import repo
from app.providers.factory import get_llm
from app.handlers.keyboards import (
    BTN_ADVICE,
    BTN_CHARTS,
    BTN_GOAL,
    BTN_HEALTH,
    BTN_HELP,
    BTN_LAST,
    BTN_REPEAT,
    BTN_SUPPORT,
    BTN_TODAY,
    MENU_BUTTONS,
    charts_kb,
    main_menu_kb,
    repeat_kb,
    start_grid_kb,
)
from app.handlers.render import day_table_data, meals_table_data
from app.services.tables import GREEN, ORANGE, send_table

log = logging.getLogger(__name__)
router = Router(name="commands")

HELP = """🏃 ⌚ 🔥 *Трекер еды и здоровья*

🍽 *Записать еду*
Пришли *фото*, *голосовое* или *текст* — «съел 200 г гречки и грудку 150».
Покажу состав и калории, подтвердишь кнопкой.

📲 *Посмотреть*
Кнопками внизу или командой — что удобнее:
📊 /today — сколько съел и потратил, вес, сон, дистанция
📈 /health — статистика за неделю и графики
🏥 /labs — показатели крови и их разбор
🫂 /psy — сон, пульс и состояние
🎓 /advice — что поправить в питании и режиме
🎯 /goal — твоя цель по здоровью

💬 *Спросить*
Любой вопрос своими словами — отвечу цифрами из твоих данных:
«сколько белка за неделю?», «как сон влияет на то, сколько я ем?»

🔔 *Напоминания и оповещения*
🌅 *12:00* — итоги вчерашнего дня таблицей и тренд неделя к неделе
🌙 *00:00* — как прошёл день и что поправить завтра
📅 *понедельник 12:10* — разбор динамики за две недели
🫂 *11:30 и 20:30* — если пульс выше твоей нормы, ВСР ниже
   или спал меньше 5 часов, напишу и разберу почему
⚖️ *12:30 и 21:30* — если вес пошёл против цели больше чем на 1 кг
🏋 если 7 дней не взвешивался — напомню встать на весы
⚠️ *12:15* — если за вчера не пришли данные из Apple Health"""




@router.message(F.text.in_(MENU_BUTTONS))
async def guard_menu_while_clarifying(message: Message) -> None:
    """Кнопка нажата, пока граф ждёт граммовку.

    commands.router подключён раньше meals.router, поэтому без этой заглушки
    текст кнопки перехватила бы команда, а уточнение осталось бы висеть.
    """
    from app.handlers.meals import is_awaiting_grams

    if not is_awaiting_grams(message.from_user.id):
        raise SkipHandler
    await message.answer(
        "Сначала допиши вес — или напиши «отмена», чтобы бросить эту запись."
    )


# Обложка лежит в образе, а не на диске сервера: так она не теряется при
# пересборке и не зависит от того, что смонтировано в контейнер.
COVER = Path(__file__).resolve().parent.parent / "assets" / "start.jpg"


# Токен заявки между нажатием ссылки и «Поделиться номером».
# В памяти, а не в базе: живёт секунды, и переживать рестарт незачем.
_auth_tokens: dict[int, str] = {}


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("auth_")))
async def cmd_start_auth(message: Message) -> None:
    """Вход в приложение: просим поделиться номером, чтобы Телеграм его
    подтвердил сам. Вводить номер руками нельзя — так его можно подделать.
    """
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    token = (message.text or "").split(maxsplit=1)[-1].removeprefix("/start ")
    token = token.removeprefix("auth_").strip()
    _auth_tokens[message.from_user.id] = token

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером",
                                  request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)
    await message.answer(
        "Чтобы войти в приложение, подтверди номер телефона — нажми кнопку ниже.\n\n"
        "Телеграм передаст номер сам, вводить его не нужно.",
        reply_markup=kb)


@router.message(F.contact)
async def on_contact(message: Message) -> None:
    """Пользователь поделился номером: сверяем с заявкой и присылаем код."""
    from app.services import phone_auth

    token = _auth_tokens.pop(message.from_user.id, None)
    if not token:
        await message.answer("Начни вход из приложения — там появится кнопка "
                             "«Открыть бота».", reply_markup=main_menu_kb())
        return

    contact = message.contact
    # Чужой контакт из адресной книги не годится: подтверждать нужно свой номер
    if contact.user_id != message.from_user.id:
        await message.answer("Нужен твой собственный номер — нажми кнопку "
                             "«Поделиться номером», а не выбирай контакт.")
        return

    res = await phone_auth.confirm_phone(token, message.from_user.id,
                                        contact.phone_number)
    if not res.get("ok"):
        if res.get("error") == "not_match":
            await message.answer(
                "Номер в Телеграме не совпал с тем, что ввели в приложении. "
                "Проверь номер и начни вход заново.",
                reply_markup=main_menu_kb())
        else:
            await message.answer(res.get("error", "Не получилось"),
                                 reply_markup=main_menu_kb())
        return

    # Код в <code>: тап по такому блоку копирует его целиком, если человек
    # хочет вставить вручную. Автоподстановку над клавиатурой iOS даёт только
    # для SMS, из мессенджера коды не вылавливает. Кнопки вставки в приложении
    # нет намеренно: в буфере часто оставался код прошлой попытки.
    await message.answer(
        f"Код для входа: <code>{res['code']}</code>\n\n"
        "Введи его в приложении — вход произойдёт сам, кнопку жать "
        "не нужно. Действует 10 минут.",
        parse_mode="HTML", reply_markup=main_menu_kb())


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # Обложка первой и БЕЗ подписи: название и слоган уже нарисованы на самой
    # картинке, а caption их дублировал. Дальше идёт справка отдельным
    # сообщением — её всё равно нельзя вложить в подпись целиком (лимит 1024).
    # Ошибку глушим: /start не должен падать из-за картинки.
    if COVER.exists():
        try:
            await message.answer_photo(FSInputFile(COVER))
        except Exception:
            log.exception("Не удалось отправить обложку")
    else:
        log.warning("Обложка не найдена: %s", COVER)

    # сначала закрепляем нижнее меню, потом сетку разделов отдельным сообщением:
    # у одного сообщения не может быть и reply-, и inline-клавиатуры
    await message.answer(HELP, parse_mode="Markdown", reply_markup=main_menu_kb())
    await message.answer("👇 *Разделы*", parse_mode="Markdown", reply_markup=start_grid_kb())


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, parse_mode="Markdown", reply_markup=main_menu_kb())


# Логика разделов вынесена из хендлеров: user_id передаётся явно, потому что
# у callback'а message.from_user — это бот, а не тот, кто нажал кнопку.

async def _today() -> date:
    """Сегодня по мнению базы, а не контейнера.

    Контейнер в UTC, вьюхи группируют по Europe/Moscow: с 00:00 до 03:00 МСК
    date.today() отставал на сутки, и кнопка «Сегодня» показывала вчерашний
    день. Берём дату там же, где считаются данные.
    """
    try:
        rows = await repo.fetch(
            "SELECT (now() AT TIME ZONE 'Europe/Moscow')::date AS d"
        )
        return rows[0]["d"]
    except Exception:
        log.exception("Не удалось взять дату из базы")
        return date.today()


async def show_day(message: Message, user_id: int, day: date, label: str) -> None:
    """Сводка за конкретный день.

    Вызывается из «показать за вчера» и из выбора даты. Метка отдельная от
    today: show_today считает себя сам, и общая метка удваивала бы счёт.
    """
    from app import metrics

    metrics.inc("sections", section="day")
    rows = await repo.fetch(
        "SELECT * FROM v_daily_nutrition WHERE user_id = %s AND day = %s",
        (user_id, day),
    )
    health = await repo.fetch(
        "SELECT * FROM v_daily_full WHERE user_id = %s AND day = %s", (user_id, day)
    )
    goals = await repo.get_active_goals(user_id)
    data = day_table_data(rows[0] if rows else None, goals,
                          health[0] if health else None, label)
    if data:
        await send_table(message, **data)
    else:
        await message.answer(f"За {label.lower()} данных пока нет.")

    # График из «Сегодня» убран: здесь смотрят цифры за один день, а недельные
    # линии к ним ничего не добавляли. Графики остались в «Здоровье».
    if meals := await _day_meals(user_id, day):
        data = meals_table_data(meals, label.lower())
        await send_table(message, **data)

    # совет последним: он опирается на те же цифры, что уже показаны выше
    if hint := await _day_hint(user_id, day, rows[0] if rows else None,
                               health[0] if health else None):
        await message.answer(f"💡 {hint}")
    return bool(rows or health)


def section_trace(section: str, user_id: int):
    """Корневой span на открытие раздела.

    Разделы зовут модель напрямую (совет по дню, разбор анализов, состояние),
    и без общего родителя эти вызовы падали в Langfuse одиночными трейсами.
    С обёрткой видно всё обращение целиком: какие запросы к базе, сколько
    вызовов модели и сколько это заняло.
    """
    from app import tracing

    return tracing.trace_request(f"раздел: {section}", user_id=user_id,
                                 text=f"[кнопка {section}]", section=section)


async def _day_hint(user_id: int, day: date, row: dict | None,
                    health: dict | None) -> str | None:
    """Совет по цифрам дня — одна-две фразы под таблицей.

    Отдельный вызов модели, а не переиспользование вечерней сводки: там акцент
    на «что поправить завтра», а здесь день ещё идёт и совет должен быть
    выполним сегодня.

    None, если советовать нечего: без калорий и активности любая рекомендация
    будет выдумкой.
    """
    h = health or {}
    eaten = (row or {}).get("kcal")
    active, resting = h.get("active_kcal"), h.get("resting_kcal")

    # утром данных нет вообще — молчим, а не советуем наугад
    if eaten is None and active is None:
        return None

    def val(x, digits: int = 0) -> str:
        return "нет данных" if x is None else f"{float(x):.{digits}f}"

    burned = None
    if active is not None or resting is not None:
        burned = float(active or 0) + float(resting or 0)
    balance = None
    if eaten is not None and burned is not None:
        balance = float(eaten) - burned

    goals = await repo.get_active_goals(user_id)
    goal = goals[0] if goals else {}

    try:
        text = await get_llm().complete(
            prompts.DAY_HINT_SYSTEM,
            prompts.DAY_HINT_USER.format(
                day=f"{day:%d.%m}",
                eaten=val(eaten),
                burned=val(burned),
                active=val(active),
                resting=val(resting),
                balance="нет данных" if balance is None else f"{balance:+.0f}",
                protein=val((row or {}).get("protein")),
                fat=val((row or {}).get("fat")),
                carbs=val((row or {}).get("carbs")),
                steps=val(h.get("steps")),
                distance=val(h.get("distance_km"), 1),
                sleep=val(h.get("sleep_hours"), 1),
                weight=val(h.get("weight_kg"), 1),
                goal=goal.get("goal_text") or "не задана",
                kcal_target=goal.get("kcal_target") or "не задана",
            ),
        )
    except Exception:
        # совет — не главное в разделе: таблица уже отправлена, молча пропускаем
        log.exception("Не удалось получить совет по дню")
        return None

    return (text or "").strip() or None


async def _day_meals(user_id: int, day: date) -> list[dict]:
    """Приёмы пищи за конкретный день, по московскому времени."""
    return await repo.fetch(
        """
        SELECT m.id, (m.eaten_at AT TIME ZONE 'Europe/Moscow') AS eaten_at,
               round(sum(i.kcal)) AS kcal,
               string_agg(coalesce(i.resolved_name, i.name), ', ' ORDER BY i.id) AS items
        FROM meals m
        JOIN meal_items i ON i.meal_id = m.id
        WHERE m.user_id = %s
          AND (m.eaten_at AT TIME ZONE 'Europe/Moscow')::date = %s
        GROUP BY m.id, m.eaten_at
        ORDER BY m.eaten_at
        """,
        (user_id, day),
    )


async def show_today(message: Message, user_id: int) -> None:
    from app import metrics

    metrics.inc("sections", section="today")
    async with section_trace("сегодня", user_id):

        today = await _today()
        rows = await repo.fetch(
            "SELECT * FROM v_daily_nutrition WHERE user_id = %s AND day = %s",
            (user_id, today),
        )
        health = await repo.fetch(
            "SELECT * FROM v_daily_full WHERE user_id = %s AND day = %s", (user_id, today)
        )

        # Ночью и утром за сегодня обычно пусто: выгрузка Health приходит позже,
        # а поесть человек ещё не успел. Предлагаем вчерашний день, а не оставляем
        # с сообщением «данных нет» — это чаще всего то, что человек хотел увидеть.
        if not rows and not health:
            yesterday = today - timedelta(days=1)
            had = await repo.fetch(
                """
                SELECT 1 FROM v_daily_nutrition WHERE user_id = %s AND day = %s
                UNION ALL
                SELECT 1 FROM health_daily WHERE user_id = %s AND day = %s
                LIMIT 1
                """,
                (user_id, yesterday, user_id, yesterday),
            )
            if had:
                await message.answer(
                    f"За сегодня ({today:%d.%m}) данных пока нет.\n"
                    f"Показать за вчера, {yesterday:%d.%m}?",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text=f"📅 Показать за {yesterday:%d.%m}",
                                callback_data=f"day:{yesterday.isoformat()}",
                            )
                        ]]
                    ),
                )
            else:
                await message.answer(
                    f"За сегодня ({today:%d.%m}) данных пока нет, за вчера тоже.\n"
                    "Запиши еду фото, голосом или текстом."
                )
            return

        goals = await repo.get_active_goals(user_id)
        data = day_table_data(rows[0] if rows else None, goals,
                              health[0] if health else None)
        if data:
            await send_table(message, **data)
        # график убран: в «Сегодня» смотрят цифры за день, недельные линии здесь
        # лишние. Они остались в «Здоровье»
        if meals := await _day_meals(user_id, today):
            await send_table(message, **meals_table_data(meals))

        if hint := await _day_hint(user_id, today, rows[0] if rows else None,
                                   health[0] if health else None):
            await message.answer(f"💡 {hint}")


@router.callback_query(F.data.startswith("day:"))
async def on_day(cb: CallbackQuery) -> None:
    """Сводка за выбранную дату — из предложения «показать за вчера»."""
    await cb.answer()
    try:
        day = date.fromisoformat(cb.data.split(":", 1)[1])
    except ValueError:
        log.warning("Плохая дата в callback: %r", cb.data)
        return
    today = await _today()
    label = "Вчера" if day == today - timedelta(days=1) else f"{day:%d.%m.%Y}"
    await show_day(cb.message, cb.from_user.id, day, label)


async def show_last(message: Message, user_id: int) -> None:
    """Журнал съеденного — открывается мини-аппом с выбором даты.

    Раньше здесь был список последних записей с кнопками правки: каждая запись
    отдельным сообщением, и чтобы поправить обед трёхдневной давности, надо было
    сначала до него долистать. В мини-аппе есть календарь и правка позиций на
    месте, поэтому список в чате больше не нужен.
    """
    from app import metrics

    metrics.inc("sections", section="last")
    from app.config import get_settings

    base = get_settings().webapp_url
    if not base:
        # WEBAPP_URL не задан — показываем прежний список, иначе раздел пустой
        await _show_last_fallback(message, user_id)
        return

    root = base.rsplit("/webapp", 1)[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🍽 Открыть журнал",
                             web_app=WebAppInfo(url=f"{root}/webapp/journal")),
    ]])
    await message.answer(
        "*Съеденные блюда*\n\n"
        "Открой журнал, выбери день и поправь, что записалось неверно — "
        "название, вес, калорийность или БЖУ.",
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def _show_last_fallback(message: Message, user_id: int) -> None:
    """Список записей текстом — если мини-апп недоступен."""
    meals = await repo.last_meals(user_id, limit=7)
    if not meals:
        await message.answer("Записей пока нет.")
        return
    lines = [
        f"*#{m['id']}* · {m['eaten_at']:%d.%m %H:%M} · {m['kcal']:.0f} ккал\n"
        f"{m['items']}"
        for m in meals
    ]
    await message.answer("\n\n".join(lines), parse_mode="Markdown")


@router.callback_query(F.data.startswith("meal:del:"))
async def on_meal_delete(cb: CallbackQuery) -> None:
    """Удаление записи по кнопке из старого списка /last.

    Сам список заменён мини-аппом журнала, но кнопки в уже отправленных
    сообщениях остались рабочими — иначе нажатие на них молча ничего не делает.
    """
    try:
        meal_id = int(cb.data.rsplit(":", 1)[1])
    except ValueError:
        await cb.answer("Не понял запись")
        return

    if await repo.delete_meal(cb.from_user.id, meal_id):
        await cb.message.edit_text(f"🗑 Запись #{meal_id} удалена")
        await cb.answer("Удалено")
    else:
        await cb.answer("Запись не найдена")


async def show_repeat(message: Message, user_id: int) -> None:
    from app import metrics

    metrics.inc("sections", section="repeat")
    meals = await repo.last_meals(user_id, limit=5)
    if not meals:
        await message.answer("Нечего повторять — записей нет.")
        return
    await message.answer("Что повторить?", reply_markup=repeat_kb(meals))


# user_id -> ждём новую формулировку цели
_awaiting_goal: set[int] = set()


def is_awaiting_goal(user_id: int) -> bool:
    return user_id in _awaiting_goal


async def show_goal(message: Message, user_id: int) -> None:
    """Текущая цель словами. Числовые таргеты не показываем: цель формулируется
    свободно («вес равняется росту»), а калории и белок бот считает сам."""
    from app import metrics

    metrics.inc("sections", section="goal")
    goals = await repo.get_active_goals(user_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Обновить цель", callback_data="goal:edit")
        ]]
    )
    if not goals:
        _awaiting_goal.add(user_id)
        await message.answer(
            "Цель пока не задана.\n\n"
            "Напиши её следующим сообщением — своими словами, например "
            "«похудеть до 85 кг к декабрю» или «набрать мышцы, спать по 8 часов».",
            reply_markup=kb,
        )
        return

    g = goals[0]
    await message.answer(
        f"*Твоя цель*\n{g['goal_text']}\n\n"
        f"_поставлена {g['active_from']:%d.%m.%Y}_",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.callback_query(F.data == "goal:edit")
async def on_goal_edit(cb: CallbackQuery) -> None:
    _awaiting_goal.add(cb.from_user.id)
    await cb.answer()
    await cb.message.answer(
        "Напиши новую цель следующим сообщением — своими словами.\n"
        "Или задай любой другой вопрос, если передумал."
    )


@router.message(F.text & ~F.text.startswith("/"),
                lambda m: is_awaiting_goal(m.from_user.id))
async def on_goal_text(message: Message) -> None:
    """Следующее сообщение после кнопки — новая цель.

    Кнопки меню пропускаем: нажатие «Сегодня» вместо цели означает, что человек
    передумал, а не что его цель называется «📊 Сегодня».
    """
    from app.handlers.keyboards import MENU_BUTTONS

    user_id = message.from_user.id
    text = (message.text or "").strip()

    if text in MENU_BUTTONS:
        _awaiting_goal.discard(user_id)
        return

    # слишком длинное — похоже на вопрос, а не на цель
    if len(text) > 200 or text.endswith("?"):
        _awaiting_goal.discard(user_id)
        return

    await repo.set_goal(user_id, text)
    _awaiting_goal.discard(user_id)
    await message.answer(
        f"Записал цель: {text}\n\nТеперь буду учитывать её в советах и разборах."
    )


async def show_health(message: Message, user_id: int) -> None:
    """Статистика за неделю + график-обзор с выбором метрик.

    Меню выбора метрик отдельным сообщением НЕ отправляем: _send_chart уже
    прикрепляет charts_kb к фото — раньше из-за этого под графиком висели
    два одинаковых меню.
    """
    from app import metrics

    metrics.inc("sections", section="health")
    rows = await repo.fetch(
        """
        SELECT day,
               round(kcal_eaten)   AS eaten,
               round(kcal_burned)  AS burned,
               round(active_kcal)  AS active,
               round(resting_kcal) AS resting,
               round(balance)      AS balance,
               weight_kg, sleep_hours, distance_km
        FROM v_daily_full
        WHERE user_id = %s
          AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - 6
        ORDER BY day DESC
        """,
        (user_id,),
    )
    if not rows:
        await message.answer(
            "Данных из Apple Health нет.\n"
            "Настрой выгрузку по инструкции в README (Быстрые команды или Health Auto Export)."
        )
        return

    data = _metrics_table_data(rows, "Статистика за неделю")

    # Средние за 7 дней — подписью под таблицей. Отдельным моноширинным блоком
    # они шли третьим сообщением подряд; в футере картинки те же цифры стоят
    # рядом с днями, из которых посчитаны.
    avg = await repo.averages(user_id, 7)
    if avg:
        # подписи короткие: полные названия из METRICS («Потраченные калории»)
        # в подпись не влезают
        short = {
            "burned": ("трата", "ккал"), "eaten": ("съел", "ккал"),
            "weight": ("вес", "кг"), "sleep": ("сон", "ч"),
            "distance": ("путь", "км"),
        }
        parts = []
        for key, (title, unit) in short.items():
            col, _, _ = repo.METRICS[key]
            v = avg.get(col)
            if v is not None:
                parts.append(f"{title} {float(v):.1f} {unit}")
        if parts:
            data["footer"] = "в среднем за 7 дней: " + " · ".join(parts)

    await send_table(message, **data)
    # график за месяц + меню метрик (клавиатура едет вместе с фото)
    await _send_chart(message, user_id, "overview", 30)


ADVICE_DAYS = 14


def _metrics_table_data(rows: list[dict], title: str = "По дням") -> dict:
    """Данные для картинки по дням: калории, вес, сон, дистанция.

    В текстовой версии это были ДВЕ таблицы: шесть метрик не влезали в 30
    символов. У картинки такого ограничения нет, поэтому день теперь читается
    одной строкой, без прыжков между «Калориями» и «Телом и активностью».

    Баланс красим: дефицит зелёным, профицит оранжевым. Знак «+» на телефоне
    легко проскочить, а цвет виден сразу — при похудении это главная колонка.
    """
    # Заголовки словами, а не сокращениями: «трата» и «Δ» понятны автору кода,
    # но не человеку, открывшему раздел впервые.
    #
    # Значки — из DejaVu Sans, а НЕ эмодзи: matplotlib не рисует цветные эмодзи
    # (NotoColorEmoji хранит глифы растром CBDT, и он их просто не видит), а
    # 🔥🍽🏋 выходили квадратиками-заглушками. Проверено на чистом образе.
    # четвёртый элемент — цветная иконка над колонкой (services/tables.py)
    COLS = [("потратил\nккал", "burned", 0, "fire"),
            ("съел\nккал", "eaten", 0, "plate"),
            ("разница\nккал", "balance", 0, "scales"),
            ("вес\nкг", "weight_kg", 1, "weight"),
            ("сон\nчасов", "sleep_hours", 1, "bed"),
            ("прошёл\nкм", "distance_km", 1, "steps")]

    def num(v, digits: int) -> str:
        return "—" if v is None else f"{float(v):.{digits}f}"

    out_rows, out_colors = [], []
    for r in rows:
        cells = [f"{r['day']:%d.%m}"]
        colors: list[str | None] = [None]
        for title_, key, digits, _ in COLS:
            v = r.get(key)
            if key == "balance" and v is not None:
                cells.append(f"{float(v):+.0f}")
                colors.append(GREEN if float(v) < 0 else ORANGE)
            else:
                cells.append(num(v, digits))
                colors.append(None)
        out_rows.append(cells)
        out_colors.append(colors)

    return {
        "title": title,
        "header": ["дата"] + [t for t, _, _, _ in COLS],
        "rows": out_rows,
        "aligns": ["l"] + ["r"] * len(COLS),
        "colors": out_colors,
        "head_icons": ["date"] + [ic for _, _, _, ic in COLS],
    }


async def show_advice(message: Message, user_id: int) -> None:
    """Разбор последних данных и рекомендации.

    Не спрашивает ничего: пользователь нажал кнопку, значит хочет вывод сразу.
    Данные собираем сами и отдаём модели одним промптом — так дешевле и
    предсказуемее, чем гонять агента с инструментами.
    """
    from app import metrics

    metrics.inc("sections", section="advice")
    async with section_trace("рекомендации", user_id):
        status = await message.answer("Смотрю данные…")

        nutrition = await repo.fetch(
            """
            SELECT day, round(eaten_kcal) AS kcal, round(protein) AS protein,
                   round(fat) AS fat, round(carbs) AS carbs, meals_count
            FROM v_daily_nutrition
            WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
            ORDER BY day DESC
            """,
            (user_id, ADVICE_DAYS),
        )
        health = await repo.fetch(
            """
            SELECT day, steps, round(active_kcal) AS active, round(total_kcal) AS total,
                   sleep_hours, resting_hr, weight_kg, body_fat_pct
            FROM health_daily
            WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
            ORDER BY day DESC
            """,
            (user_id, ADVICE_DAYS),
        )

        if not nutrition and not health:
            await status.edit_text(
                "Пока нечего разбирать — нет ни записей о еде, ни данных из Apple Health."
            )
            return

        # Сначала факты: цель, таблицы, график. Разбор от модели приходит после —
        # так человек видит цифры, на которых он построен, и может проверить.
        goals_early = await repo.get_active_goals(user_id)
        week = await repo.fetch(
            """
            SELECT day, round(kcal_burned) AS burned, round(kcal_eaten) AS eaten,
                   round(balance) AS balance, weight_kg, sleep_hours, distance_km
            FROM v_daily_full
            WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - 6
            ORDER BY day DESC
            """,
            (user_id,),
        )
        head = "*Цель:* " + (goals_early[0]["goal_text"] if goals_early else "не задана")
        # цель остаётся в статусном сообщении: его правим edit_text, а картинку
        # в уже отправленный текст не подменить — она уходит отдельным сообщением
        await status.edit_text(head, parse_mode="Markdown")
        if week:
            await send_table(message, **_metrics_table_data(week, "Последние 7 дней"))
        # График из раздела убран: те же дни уже в таблице выше, а между цифрами и
        # разбором он вклинивался третьей картинкой. Раздел теперь короткий:
        # цель → таблица → рекомендация.

        status = await message.answer("Разбираю…")

        def table(rows: list[dict]) -> str:
            if not rows:
                return "нет данных"
            out = []
            for r in rows:
                parts = [f"{r['day']:%d.%m}"]
                for k, v in r.items():
                    if k == "day" or v is None:
                        continue
                    parts.append(f"{k}={v}")
                out.append(" ".join(parts))
            return "\n".join(out)

        goals = await repo.get_active_goals(user_id)
        facts = await repo.get_facts(user_id)
        avg = await repo.averages(user_id, ADVICE_DAYS)

        try:
            text = await get_llm().complete(
                prompts.ADVICE_SYSTEM,
                prompts.ADVICE_USER.format(
                    days=ADVICE_DAYS,
                    nutrition=table(nutrition),
                    health=table(health),
                    averages=_fmt_averages(avg),
                    goal=goals[0]["goal_text"] if goals else "не задана",
                    facts=", ".join(facts) if facts else "ничего",
                ),
            )
        except Exception:
            log.exception("Не получилось собрать рекомендации")
            await status.edit_text("Модель не ответила, попробуй ещё раз через минуту.")
            return

        clean = re.sub(r"^#{1,6}\s*", "", text.strip(), flags=re.M).replace("**", "*")
        try:
            await status.edit_text(clean or "Не получилось разобрать данные.",
                                   parse_mode="Markdown")
        except Exception:
            log.warning("Markdown в рекомендациях не разобрался, отправляю без разметки")
            await status.edit_text(clean or "Не получилось разобрать данные.")


def _fmt_averages(avg: dict | None) -> str:
    if not avg:
        return "нет данных"
    out = []
    for key, (col, label, unit) in repo.METRICS.items():
        v = avg.get(col)
        if v is None:
            continue
        out.append(f"{label}: {float(v):.1f} {unit}".rstrip())
    return "\n".join(out) if out else "нет данных"


@router.message(Command("advice"))
@router.message(F.text == BTN_ADVICE)
async def cmd_advice(message: Message) -> None:
    await show_advice(message, message.from_user.id)


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def cmd_today(message: Message) -> None:
    await show_today(message, message.from_user.id)


@router.message(Command("last"))
@router.message(F.text == BTN_LAST)
async def cmd_last(message: Message) -> None:
    await show_last(message, message.from_user.id)


@router.message(Command("repeat"))
@router.message(F.text == BTN_REPEAT)
async def cmd_repeat(message: Message) -> None:
    await show_repeat(message, message.from_user.id)


@router.callback_query(F.data.startswith("repeat:"))
async def on_repeat(cb: CallbackQuery) -> None:
    meal_id = int(cb.data.split(":")[1])
    items = await repo.meal_items(meal_id)
    if not items:
        await cb.answer("Приём пищи не найден")
        return

    payload = [
        {
            "name": i["name"],
            "resolved_name": i["resolved_name"],
            "grams": float(i["grams"]),
            "kcal": float(i["kcal"]),
            "protein": float(i["protein"]),
            "fat": float(i["fat"]),
            "carbs": float(i["carbs"]),
            "confidence": float(i["confidence"]) if i["confidence"] is not None else None,
            "food_source": "repeat",
        }
        for i in items
    ]
    new_id = await repo.save_meal(
        user_id=cb.from_user.id, source="text", items=payload, raw_input=f"повтор #{meal_id}"
    )
    kcal = sum(p["kcal"] for p in payload)
    await cb.message.edit_text(f"✅ Повторено (#{new_id}), {kcal:.0f} ккал")
    await cb.answer()


@router.message(Command("goal"))
@router.message(F.text == BTN_GOAL)
async def cmd_goal(message: Message) -> None:
    """/goal минус 5 кг к сентябрю, 1800 ккал, 130 белка

    С кнопки «🎯 Цель» аргументов нет — показываем текущую цель.
    """
    text = "" if message.text == BTN_GOAL else message.text.removeprefix("/goal").strip()
    if not text:
        await show_goal(message, message.from_user.id)
        return

    kcal = _find_number(text, r"(\d{3,4})\s*ккал")
    protein = _find_number(text, r"(\d{2,3})\s*(?:г\s*)?белк")
    await repo.set_goal(message.from_user.id, text, kcal, protein)
    await message.answer(
        f"Цель сохранена: {text}\n"
        f"Калории: {kcal or 'не указано'} · Белок: {protein or 'не указано'}"
    )


def _find_number(text: str, pattern: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return int(m.group(1)) if m else None


@router.message(Command("health"))
@router.message(F.text == BTN_HEALTH)
async def cmd_health(message: Message) -> None:
    await show_health(message, message.from_user.id)


@router.message(Command("sleep"))
async def cmd_sleep(message: Message) -> None:
    """Разбор ночи: фазы, минуты по каждой и оценка сна."""
    rows = await repo.fetch(
        """SELECT day::text, deep_min, rem_min, light_min, awake_min, score
           FROM sleep_nights WHERE user_id = %s ORDER BY day DESC LIMIT 1""",
        (message.from_user.id,))
    if not rows:
        await message.answer("Данных о фазах сна пока нет — они приходят с браслета.")
        return
    n = rows[0]

    def fmt(m):
        m = int(m or 0)
        return f"{m // 60} ч {m % 60} м" if m >= 60 else f"{m} м"

    total = (n["deep_min"] or 0) + (n["rem_min"] or 0) + (n["light_min"] or 0)
    await message.answer(
        f"*Сон за {n['day']}*\n\n"
        f"Всего: {fmt(total)}\n"
        f"Оценка сна: {int(n['score'] or 0)} из 100\n\n"
        f"🌑 Глубокий: {fmt(n['deep_min'])}\n"
        f"🌀 Быстрый (REM): {fmt(n['rem_min'])}\n"
        f"🌙 Базовый: {fmt(n['light_min'])}\n"
        f"👁 Бодрствование: {fmt(n['awake_min'])}",
        parse_mode="Markdown")


@router.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    """Источники данных: приоритет и кто что прислал последним."""
    settings = await repo.source_settings(message.from_user.id)
    titles = {
        "google": "Google Health API (браслет)",
        "shortcuts": "Команды + Apple Health",
        "app": "Приложение iOS (HealthKit)",
        "manual": "Ручной ввод",
    }
    lines = ["*Источники по приоритету*", ""]
    for src, (enabled, rank) in sorted(settings.items(), key=lambda kv: -kv[1][1]):
        mark = "✅" if enabled else "⛔️"
        lines.append(f"{mark} {titles.get(src, src)} — приоритет {rank}")

    log = await repo.fetch(
        """SELECT source, day::text,
                  to_char(at AT TIME ZONE 'Europe/Moscow', 'DD.MM HH24:MI') AS at,
                  metrics
           FROM sync_log WHERE user_id = %s ORDER BY at DESC LIMIT 5""",
        (message.from_user.id,))
    if log:
        lines += ["", "*Последние поступления*", ""]
        for r in log:
            lines.append(f"{r['at']} · {titles.get(r['source'], r['source'])}")
            lines.append(f"   {r['day']}: {r['metrics'][:90]}")
    lines += ["", "_Менять приоритет и выключать источники можно "
              "в приложении и веб-версии._"]
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("foods"))
async def cmd_foods(message: Message) -> None:
    """Каталог «Мои продукты»: что чаще всего ешь и с какой порцией."""
    rows = await repo.fetch(
        """SELECT canonical_name, kcal_100, default_grams, unit, unit_grams, hits
           FROM my_foods WHERE user_id = %s
           ORDER BY hits DESC, canonical_name LIMIT 30""",
        (message.from_user.id,))
    if not rows:
        await message.answer("Каталог пуст — блюда появятся после первых записей еды.")
        return
    lines = [f"*Мои продукты* ({len(rows)} из каталога)", ""]
    for r in rows:
        if r["unit"] == "шт" and r["unit_grams"]:
            portion = f"{round((r['default_grams'] or 0) / r['unit_grams'])} шт"
        else:
            portion = f"{round(r['default_grams'] or 0)} г"
        lines.append(f"×{r['hits']} {r['canonical_name']} — "
                     f"{round(r['kcal_100'])} ккал/100 г, порция {portion}")
    lines += ["", "_Править каталог и порядок подсказок — в приложении._"]
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("charts"))
@router.message(F.text == BTN_CHARTS)
async def cmd_charts(message: Message) -> None:
    await show_charts_menu(message)


# ------------------------------------------------- сетка разделов на /start

# разделы, которые сводятся к вопросу агенту — своей SQL-логики не требуют
_NAV_QUESTIONS = {
    "week": "сколько я съел за последние 7 дней? покажи по дням",
    "balance": "мой баланс калорий за 7 дней: съедено минус потрачено",
    "top": "какие продукты я ем чаще всего за последний месяц?",
}


@router.callback_query(F.data.startswith("nav:"))
async def on_nav(cb: CallbackQuery) -> None:
    from app.handlers.meals import is_awaiting_grams

    section = cb.data.split(":", 1)[1]

    if is_awaiting_grams(cb.from_user.id):
        await cb.answer("Сначала допиши вес", show_alert=True)
        return

    await cb.answer()

    if section in _NAV_QUESTIONS:
        from app.graph.agent import ask

        await cb.message.answer("Считаю…")
        try:
            answer, table = await ask(cb.from_user.id, _NAV_QUESTIONS[section])
        except Exception:
            log.exception("Ошибка агента в разделе %s", section)
            await cb.message.answer("Не смог посчитать — попробуй ещё раз.")
            return
        await cb.message.answer(answer)
        if table:
            try:
                await send_table(cb.message, **table)
            except Exception:
                log.exception("Не удалось отправить таблицу агента")
        return

    if section == "help":
        await cb.message.answer(HELP, parse_mode="Markdown", reply_markup=main_menu_kb())
        return

    if section == "labs":
        from app.handlers.labs import show_labs

        await show_labs(cb.message, cb.from_user.id)
        return

    if section == "psy":
        from app.handlers.psy import show_psy

        await show_psy(cb.message, cb.from_user.id)
        return

    if section == "advice":
        await show_advice(cb.message, cb.from_user.id)
        return

    if section == "charts":
        await show_charts_menu(cb.message)
        return

    sections = {
        "support": show_support,
        "today": show_today,
        "repeat": show_repeat,
        "last": show_last,
        "health": show_health,
        "goal": show_goal,
    }
    show = sections.get(section)
    if show:
        await show(cb.message, cb.from_user.id)


# ------------------------------------------------------------------- графики

async def show_charts_menu(message: Message, days: int = 30) -> None:
    await message.answer(
        f"*Графики* — период: {_period_label(days)}\n\nВыбери метрику:",
        parse_mode="Markdown",
        reply_markup=charts_kb(days),
    )


def _period_label(days: int) -> str:
    return {7: "7 дней", 30: "месяц", 90: "3 месяца", 365: "год"}.get(days, f"{days} дней")


async def _send_chart(message: Message, user_id: int, kind: str, days: int) -> None:
    """Рисует и отправляет график. Отрисовка блокирующая (matplotlib), поэтому
    уводим её в поток — иначе на длинном периоде подвиснет весь бот."""
    date_to = await _today()
    date_from = date_to - timedelta(days=days)

    png = await asyncio.to_thread(_render_sync, user_id, kind, date_from, date_to)
    if png is None:
        await message.answer(
            f"Недостаточно данных за {_period_label(days)} — нужно минимум два дня "
            "с этой метрикой."
        )
        return

    label = CHART_TITLES.get(kind) or repo.METRICS.get(kind, ("", kind, ""))[1]
    await message.answer_photo(
        BufferedInputFile(png, filename=f"{kind}.png"),
        caption=f"*{label}* · {_period_label(days)}",
        parse_mode="Markdown",
        reply_markup=charts_kb(days),
    )


CHART_TITLES = {
    "overview": "Калории, вес, активность и сон",
    "psy": "Сон, пульс и восстановление",
    "balance": "Баланс калорий",
    "weight": "Вес и состав тела",
}


def _render_sync(user_id: int, kind: str, date_from: date, date_to: date) -> bytes | None:
    """Синхронная обёртка для потока: у него нет running loop, поэтому
    поднимаем свой через asyncio.run."""
    from app.services import charts

    return asyncio.run(charts.build(user_id, kind, date_from, date_to))


@router.callback_query(F.data.startswith("chart:"))
async def on_chart(cb: CallbackQuery) -> None:
    _, kind, days = cb.data.split(":")
    await cb.answer("Рисую…")
    await _send_chart(cb.message, cb.from_user.id, kind, int(days))


@router.callback_query(F.data == "period:custom")
async def on_period_custom(cb: CallbackQuery) -> None:
    """Просим даты текстом. Метрику запоминаем из подписи к текущему графику,
    иначе после ввода дат непонятно, что рисовать."""
    await cb.answer()
    caption = (cb.message.caption or cb.message.text or "").lower()
    kind = "overview"
    for key, title in CHART_TITLES.items():
        if title.lower() in caption:
            kind = key
            break
    else:
        for key, (_, label, _u) in repo.METRICS.items():
            if label.lower() in caption:
                kind = key
                break
    _awaiting_dates[cb.from_user.id] = kind
    await cb.message.answer(
        "Пришли период двумя датами, например:\n`01.07 - 15.07` или `1.7.2026 15.7.2026`",
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("period:"))
async def on_period(cb: CallbackQuery) -> None:
    days = int(cb.data.split(":")[1])
    await cb.answer(f"Период: {_period_label(days)}")
    with suppress(TelegramBadRequest):
        await cb.message.edit_reply_markup(reply_markup=charts_kb(days))


# ------------------------------------------------------------------ поддержка

async def show_support(message: Message, user_id: int) -> None:
    """Диагностика: живы ли база, API и внешние сервисы, свежа ли выгрузка."""
    from app import metrics

    metrics.inc("sections", section="support")
    from app.services import status

    note = await message.answer("Проверяю сервисы…")
    try:
        text = await status.report(user_id)
    except Exception:
        log.exception("Диагностика упала")
        await note.edit_text("Диагностика не отработала — смотри логи бота.")
        return
    await note.edit_text(text, parse_mode="Markdown")


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    """Правка анкеты — открывает ту же форму, что при регистрации.

    Форма сама подтягивает текущие значения и не спрашивает код-приглашения:
    человек уже внутри, второй раз его подтверждать незачем.
    """
    from app.config import get_settings

    s = get_settings()
    if not s.webapp_url:
        await message.answer(
            "Правка анкеты недоступна: мини-апп не настроен (нет WEBAPP_URL)."
        )
        return

    user = None
    try:
        user = await repo.get_user(message.from_user.id)
    except Exception:
        log.exception("Не удалось прочитать профиль %s", message.from_user.id)

    if user:
        head = (
            f"*Твоя анкета*\n\n"
            f"👤 {user['name']}\n"
            f"🎂 {user.get('age') or '?'} лет\n"
            f"📏 {float(user['height_cm']):.0f} см"
            if user.get("height_cm") else f"*Твоя анкета*\n\n👤 {user['name']}"
        )
    else:
        head = "*Анкета пока не заполнена*"

    # edit=1 — форма поймёт, что это правка: подставит текущие значения и
    # спрячет поле кода
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Изменить анкету",
                             web_app=WebAppInfo(url=f"{s.webapp_url}?edit=1")),
    ]])
    await message.answer(head, parse_mode="Markdown", reply_markup=kb)


@router.message(Command("support"))
@router.message(F.text == BTN_SUPPORT)
async def cmd_support(message: Message) -> None:
    await show_support(message, message.from_user.id)


# --------------------------------------------------- график за свои даты

# Кто из пользователей сейчас вводит даты. Ключ — user_id, значение — метрика,
# на которую он смотрел, чтобы после ввода нарисовать именно её.
_awaiting_dates: dict[int, str] = {}

# внутри даты разделитель только точка или слэш: иначе в «15.07-01.07»
# регулярка съедала бы «07-01» как отдельную дату
_DATE_RE = re.compile(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?")


def parse_period(text: str) -> tuple[date, date] | None:
    """Разбирает «01.07 - 15.07», «1.7.2026 15.7.2026», «01.07-15.07».

    Год необязателен: без него берём текущий, а если интервал уехал в будущее —
    считаем, что речь о прошлом годе.
    """
    found = _DATE_RE.findall(text)
    if len(found) < 2:
        return None

    # синхронная функция, и здесь нужен только год — расхождение UTC/МСК
    # в сутках на него не влияет
    today = date.today()
    parsed: list[date] = []
    for day_s, month_s, year_s in found[:2]:
        year = int(year_s) if year_s else today.year
        if year < 100:
            year += 2000
        try:
            parsed.append(date(year, int(month_s), int(day_s)))
        except ValueError:
            return None

    start, end = sorted(parsed)
    if not found[0][2] and start > today:
        start = start.replace(year=start.year - 1)
        end = end.replace(year=end.year - 1)
    if start > end:
        start, end = end, start
    return start, end


@router.message(F.text.func(lambda t: bool(t) and parse_period(t) is not None))
async def on_custom_period(message: Message) -> None:
    """Даты в ответ на «Свои даты». Если пользователь их не запрашивал —
    пропускаем сообщение дальше, иначе перехватим обычный текст про еду."""
    user_id = message.from_user.id
    kind = _awaiting_dates.pop(user_id, None)
    if kind is None:
        raise SkipHandler

    period = parse_period(message.text)
    start, end = period
    days = (end - start).days
    if days > 730:
        await message.answer("Слишком длинный интервал — максимум два года.")
        return

    png = await asyncio.to_thread(_render_sync, user_id, kind, start, end)
    if png is None:
        await message.answer(
            f"За {start:%d.%m.%Y} — {end:%d.%m.%Y} данных мало: нужно минимум два дня."
        )
        return
    label = CHART_TITLES.get(kind) or repo.METRICS.get(kind, ("", kind, ""))[1]
    await message.answer_photo(
        BufferedInputFile(png, filename=f"{kind}.png"),
        caption=f"*{label}* · {start:%d.%m.%Y} — {end:%d.%m.%Y}",
        parse_mode="Markdown",
        reply_markup=charts_kb(days),
    )
