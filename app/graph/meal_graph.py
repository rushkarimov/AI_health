"""Граф разбора приёма пищи.

                 ┌──────────┐
   photo ───────▶│  vision  │──┐
                 └──────────┘  │
                 ┌──────────┐  │   ┌─────────┐   ┌──────────┐   ┌────────┐
   voice ───────▶│   stt    │──┼──▶│  parse  │──▶│ resolve  │──▶│ totals │
                 └──────────┘  │   └─────────┘   └────┬─────┘   └────────┘
                 ┌──────────┐  │                      │  ▲
   text  ───────▶│  (сразу) │──┘                 нужно│  │ уточнение
                 └──────────┘                     уточнить│
                                                  ┌───▼──┴────┐
                                                  │ clarify   │ interrupt()
                                                  └───────────┘

Зачем здесь граф, а не три функции: входы разные, но дальше идёт общий путь,
а на resolve появляется настоящая развилка с циклом — ждём ответ пользователя
и возвращаемся. Порог уточнения у фото строже, чем у голоса.
"""
from __future__ import annotations

import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.config import get_settings
from app.graph.state import MealState
from app.providers.base import FoodItem, Recognition
from app.providers.factory import get_llm, get_stt, get_vision
from app.providers.parsing import parse_recognition_json
from app.prompts import VOICE_SYSTEM, VOICE_USER
from app.services.resolver import resolve_item

log = logging.getLogger(__name__)

MAX_CLARIFY_ROUNDS = 2


# ------------------------------------------------------------------- узлы

async def node_vision(state: MealState) -> dict:
    """Фото -> список позиций."""
    image = state.get("image")
    if not image:
        return {"error": "нет изображения"}
    try:
        rec = await get_vision().recognize_food(image, state.get("caption"))
    except Exception:
        log.exception("Ошибка vision")
        return {"error": "не получилось распознать фото"}
    return {"recognized": [i.model_dump() for i in rec.items], "note": rec.note}


async def node_stt(state: MealState) -> dict:
    """Голос -> текст."""
    audio = state.get("audio")
    if not audio:
        return {"error": "нет аудио"}
    try:
        text = await get_stt().transcribe(audio)
    except Exception:
        log.exception("Ошибка STT")
        return {"error": "не получилось распознать голос"}
    if not text:
        return {"error": "не разобрал, что сказано"}
    return {"transcript": text, "text": text}


async def node_parse(state: MealState) -> dict:
    """Текст -> список позиций. Для фото шаг пропускается (vision уже дал список)."""
    if state.get("recognized"):
        return {}

    text = state.get("text")
    if not text:
        return {"error": "нет текста для разбора"}

    try:
        raw = await get_llm().complete(
            VOICE_SYSTEM, VOICE_USER.format(text=text), json_mode=True
        )
    except Exception:
        log.exception("Ошибка разбора текста")
        return {"error": "не получилось разобрать фразу"}

    rec: Recognition = parse_recognition_json(raw)
    if not rec.items:
        return {"error": "не нашёл в сообщении еды"}
    return {"recognized": [i.model_dump() for i in rec.items], "note": rec.note}


def _needs_clarification(state: MealState) -> bool:
    """Уточняем ТОЛЬКО когда веса нет вообще.

    Раньше здесь стоял порог уверенности: фото с confidence ниже 0.75 уходило в
    уточнение, и вместо результата человек получал «уточни вес», не видя, что
    бот вообще распознал. Лишний шаг на каждом втором фото.
    Теперь неуверенные позиции показываются сразу с предположенной граммовкой и
    помечаются значком, а поправить их можно в редакторе — там видно название,
    ккал/100 г и вес.
    """
    items = state.get("recognized") or []
    if not items:
        return False
    if state.get("clarify_rounds", 0) >= MAX_CLARIFY_ROUNDS:
        return False

    return any(it.get("grams") is None for it in items)


async def node_clarify(state: MealState) -> dict:
    """Спрашивает у пользователя граммовку. Останавливает граф до ответа.

    interrupt() возвращает управление в хендлер; после resume значение
    приходит сюда как результат вызова.
    """
    items = state.get("recognized") or []
    threshold = get_settings().threshold_for(state.get("kind", "text"))
    unclear = [
        it
        for it in items
        if it.get("grams") is None
        or float(it.get("confidence", 1.0)) < threshold
    ]

    # Показываем оценку модели, а не только название: она почти всегда что-то
    # предполагает («рис отварной 200 г»), и раньше эти цифры терялись —
    # пользователь видел голый список продуктов и не понимал, что можно просто
    # подтвердить. Теперь достаточно ответить «норм».
    lines = []
    for it in unclear:
        grams = it.get("grams")
        lines.append(
            f"• {it['name']} — {float(grams):.0f} г?" if grams is not None
            else f"• {it['name']} — вес не понял"
        )

    answer = interrupt(
        {
            "type": "need_grams",
            "items": [it["name"] for it in unclear],
            "message": (
                "Проверь вес:\n" + "\n".join(lines) +
                "\n\n«норм» — если угадал, или напиши свои граммы "
                f"({', '.join(str(int(it['grams'])) for it in unclear if it.get('grams') is not None) or '200, 150'})"
            ),
        }
    )

    rounds = state.get("clarify_rounds", 0) + 1

    # "норм"/"ок" — пользователь согласен с оценкой, идём дальше без правок.
    # Ставим 0.9, а не 0.6: подтверждение человеком надёжнее догадки модели,
    # и при 0.6 граф уходил на второй круг уточнения (порог фото 0.75).
    if isinstance(answer, str) and answer.strip().lower() in {"норм", "ок", "ok", "да", "+", "норма"}:
        patched = [{**it, "confidence": 0.9} for it in items]
        return {"recognized": patched, "clarify_rounds": rounds}

    grams = _extract_grams(answer)
    patched = []
    gi = 0
    for it in items:
        need = it.get("grams") is None or float(it.get("confidence", 1.0)) < get_settings().threshold_for(
            state.get("kind", "text")
        )
        if need and gi < len(grams):
            patched.append({**it, "grams": grams[gi], "confidence": 0.9})
            gi += 1
        else:
            patched.append(it)
    return {"recognized": patched, "clarify_rounds": rounds}


def _extract_grams(answer) -> list[float]:
    import re

    if answer is None:
        return []
    return [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", str(answer))]


async def node_resolve(state: MealState) -> dict:
    """Каждую позицию превращает в нутриенты: кэш -> FatSecret -> LLM."""
    user_id = state["user_id"]
    items = [FoodItem(**it) for it in state.get("recognized") or []]

    resolved = []
    for item in items:
        resolved.append(await resolve_item(user_id, item))
    return {"resolved": resolved}


async def node_totals(state: MealState) -> dict:
    resolved = state.get("resolved") or []
    return {
        "totals": {
            "kcal": round(sum(r["kcal"] for r in resolved), 1),
            "protein": round(sum(r["protein"] for r in resolved), 1),
            "fat": round(sum(r["fat"] for r in resolved), 1),
            "carbs": round(sum(r["carbs"] for r in resolved), 1),
        }
    }


# --------------------------------------------------------------- маршрутизация

def route_entry(state: MealState) -> str:
    kind = state.get("kind", "text")
    if kind == "photo":
        return "vision"
    # голос, расшифрованный до графа (хендлер уже отличил вопрос от еды),
    # приходит с готовым text — второй раз STT не нужен, но kind остаётся
    # "voice", чтобы работал свой порог уточнения
    if kind == "voice" and not state.get("text"):
        return "stt"
    return "parse"


def route_after_parse(state: MealState) -> str:
    if state.get("error"):
        return END
    return "clarify" if _needs_clarification(state) else "resolve"


def route_after_clarify(state: MealState) -> str:
    return "clarify" if _needs_clarification(state) else "resolve"


# ------------------------------------------------------------------- сборка

def build_graph(checkpointer=None):
    g = StateGraph(MealState)

    g.add_node("vision", node_vision)
    g.add_node("stt", node_stt)
    g.add_node("parse", node_parse)
    g.add_node("clarify", node_clarify)
    g.add_node("resolve", node_resolve)
    # имя узла не должно совпадать с ключом state ("totals") — LangGraph это запрещает
    g.add_node("sum_totals", node_totals)

    g.add_conditional_edges(
        START, route_entry, {"vision": "vision", "stt": "stt", "parse": "parse"}
    )
    # фото и голос сходятся в общий parse (для фото он no-op)
    g.add_edge("vision", "parse")
    g.add_edge("stt", "parse")

    g.add_conditional_edges(
        "parse", route_after_parse, {"clarify": "clarify", "resolve": "resolve", END: END}
    )
    g.add_conditional_edges(
        "clarify", route_after_clarify, {"clarify": "clarify", "resolve": "resolve"}
    )
    g.add_edge("resolve", "sum_totals")
    g.add_edge("sum_totals", END)

    return g.compile(checkpointer=checkpointer)


def studio_graph(config=None):
    """Фабрика для LangGraph Studio.

    Studio передаёт RunnableConfig первым позиционным аргументом — в
    build_graph он попадал в checkpointer и ронял загрузку схемы.
    """
    return build_graph()


async def get_meal_graph():
    """Граф с постоянным checkpointer в Postgres — interrupt переживает рестарт бота."""
    saver = AsyncPostgresSaver.from_conn_string(get_settings().dsn)
    cm = saver
    checkpointer = await cm.__aenter__()
    await checkpointer.setup()
    return build_graph(checkpointer), cm
