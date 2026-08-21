"""Хендлеры еды: фото, голос, текст + склейка «фото и голосовой комментарий».

Склейка: если в течение COMBINE_WINDOW_SEC после фото приходит голос, он
уточняет тот же приём пищи, а не создаёт новый. Именно это снимает главную
слабость фото — оно хорошо видит состав и плохо угадывает порцию.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app.config import get_settings
from app.db import repo
from app.graph.meal_graph import get_meal_graph
from app.handlers.keyboards import confirm_kb, main_menu_kb
from app.handlers.render import render_meal

log = logging.getLogger(__name__)
router = Router(name="meals")


@dataclass
class Pending:
    """Ждёт подтверждения кнопкой, либо уточнения голосом."""

    thread_id: str
    kind: str
    resolved: list[dict]
    totals: dict
    raw_input: str | None = None
    photo_file_id: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    awaiting_grams: bool = False


# user_id -> последний незакрытый приём пищи
_pending: dict[int, Pending] = {}


def is_awaiting_grams(user_id: int) -> bool:
    """Ждём ли от пользователя граммовку. Нужно commands.router, чтобы не
    перехватывать кнопки меню посреди уточнения."""
    pending = _pending.get(user_id)
    return bool(pending and pending.awaiting_grams)

_graph = None
_graph_cm = None


async def _graph_instance():
    global _graph, _graph_cm
    if _graph is None:
        _graph, _graph_cm = await get_meal_graph()
    return _graph


# ------------------------------------------------------------------- запуск графа

async def _run(message: Message, payload: dict, kind: str) -> None:
    from app import tracing

    graph = await _graph_instance()
    thread_id = f"meal-{message.from_user.id}-{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}

    status = await message.answer("Считаю…")

    # Корневой span на всё распознавание: без него узлы графа (vision, parse,
    # resolve) попадали в Langfuse отдельными трейсами без связи, и дерево не
    # собиралось — а именно здесь оно полезнее всего, потому что видно, на
    # каком шаге модель ошиблась с продуктом или граммовкой.
    label = {"photo": "фото еды", "voice": "голос о еде"}.get(kind, "текст о еде")
    async with tracing.trace_request(
        label,
        user_id=message.from_user.id,
        text=payload.get("caption") or payload.get("text") or f"[{kind}]",
        kind=kind,
    ) as finish:
        result = await graph.ainvoke(
            {"user_id": message.from_user.id, "kind": kind,
             "clarify_rounds": 0, **payload},
            config=config,
        )
        items = result.get("resolved") or []
        finish(output={
            "позиции": [i.get("resolved_name") or i.get("name") for i in items],
            "ккал": (result.get("totals") or {}).get("kcal"),
        } if items else {"ошибка": result.get("error") or "уточнение"})

    await _handle_result(message, status, graph, config, thread_id, kind, result, payload)


async def _pending_interrupt(graph, config: dict, result: dict) -> dict | None:
    """Значение interrupt(), если граф встал на уточнении.

    В LangGraph 0.2.60 результат ainvoke НЕ содержит ключа "__interrupt__" —
    он появился в более поздних версиях. Поэтому сначала пробуем его (на случай
    обновления), а затем читаем состояние потока: там interrupt лежит в
    state.tasks[].interrupts. Без этого бот не замечал остановку графа и
    отвечал «Не нашёл, что записать» на каждое фото с низкой уверенностью.
    """
    if interrupts := result.get("__interrupt__"):
        return interrupts[0].value

    try:
        state = await graph.aget_state(config)
    except Exception:
        log.exception("Не удалось прочитать состояние графа")
        return None

    for task in state.tasks or ():
        for interrupt in getattr(task, "interrupts", None) or ():
            value = getattr(interrupt, "value", None)
            if isinstance(value, dict):
                return value
    return None


async def _handle_result(
    message: Message,
    status: Message,
    graph,
    config: dict,
    thread_id: str,
    kind: str,
    result: dict,
    payload: dict,
) -> None:
    if err := result.get("error"):
        await status.edit_text(f"Не получилось: {err}")
        return

    # граф остановился на interrupt — нужно уточнение
    if info := await _pending_interrupt(graph, config, result):
        _pending[message.from_user.id] = Pending(
            thread_id=thread_id,
            kind=kind,
            resolved=[],
            totals={},
            raw_input=result.get("transcript") or payload.get("text"),
            photo_file_id=payload.get("photo_file_id"),
            awaiting_grams=True,
        )
        await status.edit_text(info.get("message", "Уточни вес"))
        return

    resolved = result.get("resolved") or []
    if not resolved:
        await status.edit_text("Не нашёл, что записать. Попробуй ещё раз.")
        return

    _pending[message.from_user.id] = Pending(
        thread_id=thread_id,
        kind=kind,
        resolved=resolved,
        totals=result.get("totals", {}),
        raw_input=result.get("transcript") or payload.get("text"),
        photo_file_id=payload.get("photo_file_id"),
    )
    # Черновик в базе + ссылка на редактор: мини-апп живёт в API-контейнере и
    # список блюд из памяти бота не увидит, поэтому передаём через базу.
    edit_url = await _draft_url(
        message.from_user.id, resolved, kind,
        raw_input=result.get("transcript") or payload.get("text"),
        photo_file_id=payload.get("photo_file_id"),
    )
    await status.edit_text(
        render_meal(resolved, result.get("totals", {}), result.get("note")),
        reply_markup=confirm_kb(edit_url),
    )


async def _resume(message: Message, answer: str) -> None:
    """Продолжает граф после уточнения."""
    from langgraph.types import Command

    pending = _pending.get(message.from_user.id)
    if not pending:
        return

    graph = await _graph_instance()
    config = {"configurable": {"thread_id": pending.thread_id}}
    status = await message.answer("Пересчитываю…")
    result = await graph.ainvoke(Command(resume=answer), config=config)
    await _handle_result(
        message, status, graph, config, pending.thread_id, pending.kind, result,
        {"text": pending.raw_input, "photo_file_id": pending.photo_file_id},
    )


async def _draft_url(user_id: int, items: list[dict], kind: str,
                     raw_input: str | None = None,
                     photo_file_id: str | None = None,
                     meal_id: int | None = None) -> str | None:
    """Кладёт черновик в базу и возвращает ссылку на редактор.

    None, если WEBAPP_URL не задан: без него кнопку правки показывать нечем.
    Ошибки глушим — подтверждение записи не должно падать из-за редактора.
    """
    import secrets

    from app.config import get_settings

    base = get_settings().webapp_url
    if not base:
        return None

    draft_id = secrets.token_urlsafe(9)
    try:
        await repo.create_draft(draft_id, user_id, items, kind,
                                raw_input=raw_input, photo_file_id=photo_file_id,
                                meal_id=meal_id)
    except Exception:
        log.exception("Не удалось создать черновик для %s", user_id)
        return None

    # base указывает на /webapp (форма регистрации) — редактор рядом
    root = base.rsplit("/webapp", 1)[0]
    return f"{root}/webapp/meal?draft={draft_id}"


# ------------------------------------------------------------------- хендлеры

@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    photo = message.photo[-1]  # максимальное разрешение
    buf = await bot.download(photo.file_id)
    await _run(
        message,
        {
            "image": buf.read(),
            "caption": message.caption,
            "photo_file_id": photo.file_id,
        },
        "photo",
    )


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id
    pending = _pending.get(user_id)

    # ждём граммовку — голос идёт как ответ на уточнение
    if pending and pending.awaiting_grams:
        from app.providers.factory import get_stt

        voice = message.voice or message.audio
        buf = await bot.download(voice.file_id)
        try:
            text = await get_stt().transcribe(buf.read())
        except Exception:
            log.exception("Ошибка STT при уточнении")
            await message.answer("Не разобрал голос, напиши цифрами")
            return
        await message.answer(f"Услышал: {text}")
        await _resume(message, text)
        return

    # голос вскоре после фото — уточняет тот же приём пищи
    window = get_settings().combine_window_sec
    if pending and (time.monotonic() - pending.created_at) < window and pending.kind == "photo":
        from app.providers.factory import get_stt

        voice = message.voice or message.audio
        buf = await bot.download(voice.file_id)
        try:
            text = await get_stt().transcribe(buf.read())
        except Exception:
            log.exception("Ошибка STT при склейке")
            text = ""
        if text:
            await message.answer(f"Уточняю фото: «{text}»")
            await _run(
                message,
                {"text": text, "photo_file_id": pending.photo_file_id},
                "combined",
            )
            return

    # обычное голосовое: сначала расшифровываем, потом решаем — вопрос это или еда.
    # Раньше голос всегда уходил в разбор еды, и «сколько я съел за неделю?»
    # бот пытался съесть.
    from app.providers.factory import get_stt

    voice = message.voice or message.audio
    buf = await bot.download(voice.file_id)
    audio = buf.read()

    try:
        text = await get_stt().transcribe(audio)
    except Exception:
        log.exception("Ошибка STT")
        await message.answer("Не разобрал голос — попробуй ещё раз или напиши текстом.")
        return

    if not text.strip():
        await message.answer("Тишина в записи — скажи ещё раз.")
        return

    # голос идёт в тот же роутер, что и текст; STT второй раз не гоняем
    await message.answer(f"Услышал: «{text}»")
    await _route_text(message, text, source="voice")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    pending = _pending.get(message.from_user.id)
    if pending and pending.awaiting_grams:
        # выход из зависшего уточнения без кнопок
        if message.text.strip().lower() in {"отмена", "отменить", "стоп"}:
            _pending.pop(message.from_user.id, None)
            await message.answer("Отменено", reply_markup=main_menu_kb())
            return
        await _resume(message, message.text)
        return

    await _route_text(message, message.text)


async def _route_text(message: Message, text: str, source: str = "text") -> None:
    """Куда направить сообщение — решает граф-роутер (LLM, не регулярки).

    Две ветки: agent (общение, вопросы по базе, будущие тулзы) и food (запись
    еды через meal_graph). Запись запускаем здесь, а не внутри router_graph:
    у meal_graph есть interrupt() для уточнения граммовки, и вложенный вызов
    графа с прерыванием усложнил бы возобновление.
    """
    from app.graph.router_graph import classify

    status = await message.answer("Секунду…")
    try:
        result = await classify(message.from_user.id, text, source)
    except Exception:
        log.exception("Роутер упал — считаем это едой")
        await status.delete()
        await _run(message, {"text": text}, source)
        return

    if result.get("to_food"):
        await status.delete()
        await _run(message, {"text": text}, source)
        return

    answer = (result.get("answer") or "").strip()
    await status.edit_text(answer or "Не понял — переспроси, пожалуйста.")
    # Таблица приходит отдельной картинкой: в текст её не вложить, а
    # status.edit_text уже занят ответом модели.
    if table := result.get("table"):
        from app.services.tables import send_table

        try:
            await send_table(message, **table)
        except Exception:
            log.exception("Не удалось отправить таблицу агента")


_SLOT_WORDS = {
    "завтрак": "09:00",
    "обед": "13:30",
    "ужин": "19:30",
    "перекус": "23:30",
}


def _slot_from_text(text: str | None) -> str | None:
    """«эти блюда в завтрак» в тексте или подписи к фото выбирает приём.

    Возвращает eaten_local вида «YYYY-MM-DD HH:MM» или None — тогда
    приём запишется текущим временем, как раньше.
    """
    if not text:
        return None
    low = text.lower()
    for word, hhmm in _SLOT_WORDS.items():
        if word in low:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d")
            return f"{today} {hhmm}"
    return None


# ------------------------------------------------------------------- кнопки

@router.callback_query(F.data == "meal:save")
async def on_save(cb: CallbackQuery) -> None:
    pending = _pending.pop(cb.from_user.id, None)
    if not pending or not pending.resolved:
        await cb.answer("Нечего сохранять")
        return

    meal_id = await repo.save_meal(
        user_id=cb.from_user.id,
        source=pending.kind,
        items=pending.resolved,
        raw_input=pending.raw_input,
        photo_file_id=pending.photo_file_id,
        eaten_local=_slot_from_text(pending.raw_input),
    )
    kcal = pending.totals.get("kcal", 0)
    await cb.message.edit_text(
        f"{cb.message.text}\n\n✅ Записано (#{meal_id}), {kcal:.0f} ккал"
    )
    await cb.answer("Записано")


@router.callback_query(F.data == "meal:cancel")
async def on_cancel(cb: CallbackQuery) -> None:
    _pending.pop(cb.from_user.id, None)
    await cb.message.edit_text("Отменено")
    await cb.answer()


@router.callback_query(F.data == "meal:edit")
async def on_edit(cb: CallbackQuery) -> None:
    """Правка граммовки текстом — запасной путь.

    Основной способ теперь мини-апп: кнопка «Исправить» открывает форму, где
    видно название, вес и ккал/100 г. Этот хендлер срабатывает, только если
    WEBAPP_URL не задан и кнопка осталась обычной callback-кнопкой.
    """
    pending = _pending.get(cb.from_user.id)
    if not pending:
        await cb.answer("Нечего править")
        return
    pending.awaiting_grams = True
    names = ", ".join(r["resolved_name"] or r["name"] for r in pending.resolved)
    await cb.message.answer(
        f"Что поправить? Напиши новые граммы по порядку ({names}).\n"
        "Например: «200, 150»"
    )
    await cb.answer()
