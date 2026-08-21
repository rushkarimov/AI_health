"""Раздел «Анализы»: свежие показатели, разбор и добавление новых.

Как работает добавление. После кнопки «Добавить анализы» ставим флаг ожидания
и следующее сообщение — фото, голос или текст — разбираем как анализ.

Но флаг не блокирующий: если человек вместо анализов спросил «сколько я съел
сегодня», это должно уйти агенту, а не в парсер анализов. Поэтому перед
разбором классифицируем сообщение — это дешёвый вызов Flash, зато не бывает
залипания в режиме, из которого не выйти.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app import prompts
from app.db import repo
from app.handlers.keyboards import BTN_LABS, labs_kb, main_menu_kb
from app.services.tables import ORANGE, send_table
from app.providers.factory import get_llm, get_stt, get_vision
from app.providers.parsing import extract_json

log = logging.getLogger(__name__)
router = Router(name="labs")

# user_id -> ждём ли мы анализы
_awaiting: set[int] = set()


def is_awaiting_labs(user_id: int) -> bool:
    return user_id in _awaiting


def _labs_table_data(rows: list[dict], title: str) -> dict:
    """Данные для картинки с показателями.

    В текстовой версии колонку нормы приходилось выбрасывать: она съедала 10
    из 30 доступных символов. В картинке место есть, поэтому границы нормы
    вернулись — без них непонятно, насколько именно значение вышло за предел.

    Отклонения красим целиком по строке, а стрелки ↓↑ оставляем: цвет виден
    сразу, но по стрелке понятно направление и в чёрно-белом скриншоте.
    """
    def short(v) -> str:
        """Число без хвостовых нулей: 5.4 вместо 5.40, 152 вместо 152.0."""
        return "—" if v is None else f"{float(v):g}"

    out_rows, out_colors = [], []
    for r in rows:
        flag = r.get("flag") or ""
        name = r.get("canonical") or r.get("name") or "?"
        mark = {"low": "↓ ", "high": "↑ "}.get(flag, "")

        value = short(r.get("value"))
        if r.get("unit"):
            value += " " + str(r["unit"])

        lo, hi = r.get("ref_low"), r.get("ref_high")
        if lo is not None and hi is not None:
            norm = f"{short(lo)}–{short(hi)}"
        elif hi is not None:
            norm = f"до {short(hi)}"
        elif lo is not None:
            norm = f"от {short(lo)}"
        else:
            norm = "—"

        color = ORANGE if flag in ("low", "high") else None
        out_rows.append([f"{mark}{name}", value, norm])
        out_colors.append([color, color, None])

    return {
        "title": title,
        "header": ["показатель", "значение", "норма"],
        "rows": out_rows,
        "aligns": ["l", "r", "r"],
        "colors": out_colors,
        "head_icons": ["blood", "chart", "target"],
    }


async def show_labs(message: Message, user_id: int) -> None:
    """Свежая сдача + кнопка добавить новую."""
    from app import metrics

    metrics.inc("sections", section="labs")
    rows = await repo.last_labs(user_id)
    if not rows:
        _awaiting.add(user_id)
        await message.answer(
            "Анализов пока нет.\n\n"
            "Пришли фото бланка, продиктуй голосом или напиши текстом — "
            "например «гемоглобин 145, ферритин 30, витамин D 22».\n\n"
            "Или просто задай другой вопрос, если передумал.",
            reply_markup=main_menu_kb(),
        )
        return

    taken = rows[0]["taken_on"]
    off = [r for r in rows if r.get("flag") in ("low", "high")]

    # заголовок с датой и лабораторией уходит в шапку картинки, счётчики —
    # в подпись под таблицей: в тексте сообщения дублировать их незачем
    title = f"Анализы от {taken:%d.%m.%Y}"
    if rows[0].get("lab"):
        title += f" · {rows[0]['lab']}"

    data = _labs_table_data(rows, title)
    footer = f"показателей: {len(rows)}"
    if off:
        footer += f" · вне нормы: {len(off)}"
    data["footer"] = footer

    caption = ""
    history = await repo.lab_dates(user_id)
    if len(history) > 1:
        caption = "другие сдачи: " + ", ".join(
            f"{h['taken_on']:%d.%m}" for h in history[1:6]
        )

    await send_table(message, **data, caption=caption, reply_markup=labs_kb())


async def explain_labs(message: Message, user_id: int) -> None:
    """Разбор свежей сдачи с учётом питания и активности."""
    rows = await repo.last_labs(user_id)
    if not rows:
        await message.answer("Сначала добавь анализы — пришли фото или напиши текстом.")
        return

    status = await message.answer("Разбираю…")
    labs_text = "\n".join(
        f"- {r.get('canonical') or r['name']}: {r['value']} {r.get('unit') or ''}"
        f" (норма {r.get('ref_low') or '?'}–{r.get('ref_high') or '?'})"
        f"{' ВНЕ НОРМЫ' if r.get('flag') in ('low', 'high') else ''}"
        for r in rows
    )

    ctx = await repo.fetch(
        """
        SELECT day, round(kcal_eaten) AS eaten, round(kcal_burned) AS burned,
               round(protein) AS protein, sleep_hours, weight_kg
        FROM v_daily_full
        WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - 14
        ORDER BY day DESC
        """,
        (user_id,),
    )
    context = "\n".join(
        " ".join(
            [f"{r['day']:%d.%m}"]
            + [f"{k}={v}" for k, v in r.items() if k != "day" and v is not None]
        )
        for r in ctx
    ) or "нет данных"

    goals = await repo.get_active_goals(user_id)
    try:
        text = await get_llm().complete(
            prompts.LABS_ADVICE_SYSTEM,
            prompts.LABS_ADVICE_USER.format(
                taken_on=f"{rows[0]['taken_on']:%d.%m.%Y}",
                labs=labs_text,
                context=context,
                goal=goals[0]["goal_text"] if goals else "не задана",
            ),
        )
    except Exception:
        log.exception("Не получилось разобрать анализы")
        await status.edit_text("Модель не ответила, попробуй ещё раз через минуту.")
        return

    clean = re.sub(r"^#{1,6}\s*", "", text.strip(), flags=re.M).replace("**", "*")
    try:
        await status.edit_text(clean, parse_mode="Markdown")
    except Exception:
        await status.edit_text(clean)


async def ingest(message: Message, bot: Bot, text: str | None = None) -> bool:
    """Разбирает присланные анализы. True — если это были анализы.

    False означает «сообщение не про анализы»: тогда вызывающий код пропускает
    его дальше по цепочке хендлеров, и человек не залипает в режиме ввода.
    """
    user_id = message.from_user.id
    source = "text"
    extra = ""

    if message.photo:
        source = "photo"
        buf = await bot.download(message.photo[-1].file_id)
        image = buf.read()
        try:
            raw = await get_vision().recognize_labs(image)
        except Exception:
            log.exception("Vision не разобрал бланк анализов")
            await message.answer("Не смог прочитать бланк. Попробуй снимок поярче.")
            return True
    else:
        body = text or message.text or ""
        if not body.strip():
            return False
        extra = f"Текст от пользователя:\n{body}"
        try:
            raw = await get_llm().complete(
                prompts.LABS_PARSE_SYSTEM,
                prompts.LABS_PARSE_USER.format(
                    today=date.today().isoformat(), extra=extra
                ),
                json_mode=True,
            )
        except Exception:
            log.exception("Модель не разобрала анализы из текста")
            await message.answer("Не получилось разобрать. Попробуй ещё раз.")
            return True

    data = extract_json(raw) or {}
    items = data.get("items") if isinstance(data, dict) else None
    if not items:
        return False

    taken_on = date.today()
    if raw_date := (data.get("taken_on") if isinstance(data, dict) else None):
        try:
            taken_on = date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            log.warning("Не разобрал дату сдачи %r, ставлю сегодня", raw_date)

    saved = await repo.save_labs(
        user_id, taken_on, items,
        lab=data.get("lab"), source=source,
        raw_input=(text or message.text or message.caption),
    )
    _awaiting.discard(user_id)

    await message.answer(f"Записал {saved} показателей за {taken_on:%d.%m.%Y}.")
    await show_labs(message, user_id)
    return True


@router.message(Command("labs"))
@router.message(F.text == BTN_LABS)
async def cmd_labs(message: Message) -> None:
    await show_labs(message, message.from_user.id)


@router.callback_query(F.data == "labs:add")
async def on_labs_add(cb: CallbackQuery) -> None:
    _awaiting.add(cb.from_user.id)
    await cb.answer()
    await cb.message.answer(
        "Пришли анализы: фото бланка, голосовое или текстом.\n"
        "Например: «гемоглобин 145, ферритин 30, витамин D 22».\n\n"
        "Или задай любой другой вопрос — я пойму, что ты передумал."
    )


# --- перехват ввода, пока ждём анализы ---
# Роутер labs зарегистрирован раньше meals, поэтому эти хендлеры получают
# сообщение первыми. Если это не анализы — возвращаем управление дальше,
# и человек не залипает в режиме ввода.

@router.message(F.photo, lambda m: is_awaiting_labs(m.from_user.id))
async def on_labs_photo(message: Message, bot: Bot) -> None:
    if not await ingest(message, bot):
        _awaiting.discard(message.from_user.id)
        await message.answer("Это не похоже на бланк анализов — отправляю как еду.")
        from app.handlers.meals import on_photo

        await on_photo(message, bot)


@router.message(F.voice | F.audio, lambda m: is_awaiting_labs(m.from_user.id))
async def on_labs_voice(message: Message, bot: Bot) -> None:
    voice = message.voice or message.audio
    buf = await bot.download(voice.file_id)
    try:
        text = await get_stt().transcribe(buf.read())
    except Exception:
        log.exception("STT не разобрал голос с анализами")
        await message.answer("Не разобрал голос, напиши текстом.")
        return
    await message.answer(f"Услышал: {text}")
    if not await ingest(message, bot, text=text):
        _awaiting.discard(message.from_user.id)
        await _fallback(message, text)


@router.message(F.text & ~F.text.startswith("/"), lambda m: is_awaiting_labs(m.from_user.id))
async def on_labs_text(message: Message, bot: Bot) -> None:
    from app.handlers.keyboards import MENU_BUTTONS

    # нажатие кнопки меню — явный выход из режима, без обращения к модели
    if message.text in MENU_BUTTONS:
        _awaiting.discard(message.from_user.id)
        return
    if not await ingest(message, bot):
        _awaiting.discard(message.from_user.id)
        await _fallback(message, message.text)


async def _fallback(message: Message, text: str) -> None:
    """Сообщение оказалось не про анализы — отдаём обычному маршруту."""
    from app.graph.router_graph import classify

    result = await classify(message.from_user.id, text)
    if answer := result.get("answer"):
        await message.answer(answer)
        if table := result.get("table"):
            try:
                await send_table(message, **table)
            except Exception:
                log.exception("Не удалось отправить таблицу агента")
        return
    # роутер решил, что это еда — запускаем разбор еды
    from app.handlers.meals import _run

    await _run(message, {"text": text}, "text")


@router.callback_query(F.data == "labs:explain")
async def on_labs_explain(cb: CallbackQuery) -> None:
    await cb.answer()
    await explain_labs(cb.message, cb.from_user.id)
