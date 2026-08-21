"""Граф верхнего уровня: куда направить сообщение.

              ┌──────────┐
   текст ────▶│  router  │──┬──▶ agent  ──▶ END   (общение + вопросы по БД)
   голос      └──────────┘  │
                            └──▶ food   ──▶ END   (запись еды через meal_graph)

Классифицирует модель, а не регулярки: раньше здесь был список стоп-слов, и он
предсказуемо мазал на фразах вида «а можно мне 200 грамм риса?» — есть «мне»
и вопросительный знак, значит уходило в вопросы вместо записи.

Ветки всего две. «Переспросить, если непонятно» — не отдельный узел, а поведение
самого агента: у него есть инструменты, и он сам решает, ответить словами,
уточнить вопрос или сходить в базу.

Checkpointer здесь НЕ используется: у этого графа нет interrupt и нет цикла,
состояние между сообщениями не нужно. Запись еды дальше идёт в meal_graph,
у которого свой постоянный checkpointer в Postgres.
"""
from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from app import prompts, tracing
from app.providers.factory import get_llm
from app.providers.parsing import extract_json

log = logging.getLogger(__name__)

Intent = Literal["food", "agent"]


class RouterState(TypedDict, total=False):
    user_id: int
    text: str
    source: str            # text | voice
    intent: Intent
    reason: str
    answer: str            # ответ агента, если ветка agent
    table: dict            # данные таблицы для картинки, если агент их собрал
    to_food: bool          # ветка food: хендлер запустит meal_graph


async def node_router(state: RouterState) -> dict:
    """Классификация одним дешёвым вызовом Flash."""
    async with tracing.trace_step("node:router"):
        return await _classify_text(state)


async def _classify_text(state: RouterState) -> dict:
    text = (state.get("text") or "").strip()
    if not text:
        return {"intent": "agent", "reason": "пустое сообщение"}

    try:
        raw = await get_llm().complete(
            prompts.ROUTER_SYSTEM,
            prompts.ROUTER_USER.format(text=text),
            json_mode=True,
        )
        plan = extract_json(raw) or {}
    except Exception:
        log.exception("Роутер недоступен")
        plan = {}

    intent = plan.get("intent")
    if intent not in ("food", "agent"):
        # модель не ответила или ответила мусором: в агента, а не в запись еды.
        # Записать лишний приём пищи хуже, чем лишний раз переспросить.
        log.warning("Роутер вернул %r, уходим в agent", intent)
        intent = "agent"

    log.info("Роутер: %r -> %s (%s)", text[:60], intent, plan.get("reason", ""))
    return {"intent": intent, "reason": str(plan.get("reason") or "")}


async def node_agent(state: RouterState) -> dict:
    """Общение и вопросы по данным — ReAct-агент с инструментами."""
    from app.graph.agent import ask

    async with tracing.trace_step("node:agent", intent=state.get("intent")):
        try:
            # ask отдаёт текст и данные таблицы отдельно: таблицу хендлер
            # отправляет картинкой, в текст сообщения её не вложить
            answer, table = await ask(state["user_id"], state["text"])
        except Exception:
            log.exception("Агент упал")
            answer, table = "Не смог обработать — попробуй переформулировать.", None
    return {"answer": answer, "table": table}


async def node_food(state: RouterState) -> dict:
    """Пометка для хендлера: дальше запускается meal_graph.

    Сам meal_graph здесь не вызываем — у него interrupt() для уточнения
    граммовки, и вложенный вызов графа с прерыванием усложнил бы возобновление.
    """
    async with tracing.trace_step("node:food", intent=state.get("intent")):
        return {"to_food": True}


def route(state: RouterState) -> str:
    return "food" if state.get("intent") == "food" else "agent"


def build_router_graph():
    g = StateGraph(RouterState)

    g.add_node("router", node_router)
    g.add_node("agent", node_agent)
    g.add_node("food", node_food)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", route, {"agent": "agent", "food": "food"})
    g.add_edge("agent", END)
    g.add_edge("food", END)

    return g.compile()


_graph = None


def get_router_graph():
    """Скомпилированный граф — один на процесс, компиляция не бесплатная."""
    global _graph
    if _graph is None:
        _graph = build_router_graph()
    return _graph


async def classify(user_id: int, text: str, source: str = "text") -> dict[str, Any]:
    """Точка входа для хендлеров.

    Весь путь сообщения — один трейс: узлы графа и вызовы модели внутри него
    становятся вложенными span'ами, поэтому в Langfuse видна структура графа.
    """
    from app.services import memory

    async with tracing.trace_request(
        "message", user_id=user_id, text=text, source=source
    ) as finish:
        result = await get_router_graph().ainvoke(
            {"user_id": user_id, "text": text, "source": source}
        )
        # пишем и вопрос, и ответ: иначе в контексте будут одни реплики
        # пользователя, и модель не увидит, что сама уже отвечала
        await memory.remember(user_id, "user", text, result.get("intent"))
        if answer := result.get("answer"):
            await memory.remember(user_id, "assistant", answer)
        finish(
            output=result.get("answer") or ("-> meal_graph" if result.get("to_food") else None),
            intent=result.get("intent"),
            reason=result.get("reason"),
        )
        return result
