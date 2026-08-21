"""Память бота: контекст разговора и факты о пользователе.

Два уровня, потому что задачи разные.

**Короткая** — последние реплики в пределах 15 минут. Нужна, чтобы работали
уточнения вида «а сколько это в белках?»: без истории бот не понимал, о чём
речь. Окно намеренно узкое — через час тот же вопрос относится уже к другому,
и старый контекст мешает, а не помогает.

**Долгая** — факты вроде «зовут Русан», «не ест молочное». Извлекаются редко
(раз в N сообщений), потому что это отдельный вызов модели. Хранятся строками,
а не диалогом: компактно и не раздувает промпт.

Обе функции гасят свои ошибки: сломанная память не должна ломать ответ.
"""
from __future__ import annotations

import logging

from app import prompts
from app.db import repo
from app.providers.factory import get_llm
from app.providers.parsing import extract_json

log = logging.getLogger(__name__)

# Как часто выделяем факты. Каждое сообщение — дорого и незачем: человек не
# сообщает о себе новое в каждой реплике.
FACTS_EVERY_N_MESSAGES = 6
# Сколько реплик отдаём модели на разбор фактов
FACTS_FROM_LAST = 12


def _strip_tables(text: str) -> str:
    """Убирает markdown-таблицы из реплики.

    В ответах бота лежат таблицы на 15 строк — в контексте они занимают почти
    весь бюджет токенов и провоцируют модель переписывать старые цифры вместо
    свежих из инструментов.
    """
    if "```" not in text:
        return text.strip()
    head = text.split("```", 1)[0].strip()
    return head or "(таблица с данными)"


async def build_context(user_id: int) -> str:
    """Блок для system-промпта: что мы знаем о человеке и о чём только что говорили.

    Пустая строка, если знать нечего — тогда промпт остаётся коротким.
    """
    parts: list[str] = []

    try:
        facts = await repo.get_facts(user_id)
    except Exception:
        log.exception("Не удалось прочитать факты")
        facts = []
    # Цель — первой строкой контекста: она задаёт рамку для любого совета,
    # от «сколько мне есть» до разбора анализов. Раньше цель попадала только
    # в планировщик и анализы, а в переписке модель о ней не знала.
    try:
        goals = await repo.get_active_goals(user_id)
    except Exception:
        log.exception("Не удалось прочитать цель")
        goals = []
    if goals:
        parts.append(f"Цель пользователя по здоровью: {goals[0]['goal_text']}")

    if facts:
        parts.append("Что ты знаешь о пользователе:\n" + "\n".join(f"- {f}" for f in facts))

    try:
        dialog = await repo.recent_dialog(user_id)
    except Exception:
        log.exception("Не удалось прочитать историю диалога")
        dialog = []
    if dialog:
        # Нужны обе стороны: без реплик бота непонятно, на что отвечает «Да»
        # или «Бег на дорожке» — модель начинала разговор заново.
        #
        # Но свои ответы кладём УРЕЗАННО и с пометкой: раньше ошибочный ответ
        # («данных за сегодня нет») закреплялся и повторялся снова, даже когда
        # данные в выборке были. Цифры модель обязана брать из инструментов,
        # а не из своей прошлой реплики.
        lines = []
        for m in dialog:
            text = _strip_tables(m["text"])
            if not text:
                continue
            who = "Пользователь" if m["role"] == "user" else "Ты"
            lines.append(f"{who}: {text[:220]}")
        if lines:
            parts.append(
                "Недавний разговор — только чтобы понять, о чём речь. "
                "Цифры из своих прошлых ответов НЕ используй, бери из инструментов:\n"
                + "\n".join(lines)
            )

    return "\n\n".join(parts)


async def remember(user_id: int, role: str, text: str, intent: str | None = None) -> None:
    """Пишет реплику в историю и изредка выделяет факты."""
    await repo.save_message(user_id, role, text, intent)

    # факты выделяем только по сообщениям пользователя и не каждый раз
    if role != "user":
        return
    try:
        rows = await repo.fetch(
            "SELECT count(*) AS n FROM messages WHERE user_id = %s AND role = 'user'",
            (user_id,),
        )
        n = int(rows[0]["n"]) if rows else 0
    except Exception:
        log.exception("Не удалось посчитать сообщения")
        return

    if n % FACTS_EVERY_N_MESSAGES == 0:
        await extract_facts(user_id)


async def extract_facts(user_id: int) -> int:
    """Отдельный вызов модели: что из разговора стоит помнить надолго."""
    try:
        rows = await repo.fetch(
            """
            SELECT text FROM (
                SELECT text, created_at FROM messages
                WHERE user_id = %s AND role = 'user'
                ORDER BY created_at DESC LIMIT %s
            ) t ORDER BY created_at
            """,
            (user_id, FACTS_FROM_LAST),
        )
    except Exception:
        log.exception("Не удалось прочитать реплики для разбора фактов")
        return 0

    dialog = "\n".join(f"- {r['text'][:300]}" for r in rows)
    if not dialog.strip():
        return 0

    known = await repo.get_facts(user_id)
    system = prompts.FACTS_SYSTEM
    if known:
        # чтобы модель не присылала то же самое снова
        system += "\n\nУже известно (не повторяй):\n" + "\n".join(f"- {f}" for f in known)

    try:
        raw = await get_llm().complete(
            system, prompts.FACTS_USER.format(dialog=dialog), json_mode=True
        )
    except Exception:
        log.exception("Модель не ответила на разбор фактов")
        return 0

    data = extract_json(raw) or {}
    facts = data.get("facts") if isinstance(data, dict) else None
    if not isinstance(facts, list):
        return 0

    return await repo.add_facts(user_id, [str(f) for f in facts if f])
