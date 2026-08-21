"""Трейсинг вызовов LLM в Langfuse.

Требует SDK 4.x: в 3.x клиент шлёт события в /api/public/ingestion, а сервер
Langfuse 4 принимает там только оценки и логи — трейсы молча терялись, хотя
auth_check проходил. В 4.x единый метод start_observation с параметром as_type
вместо раздельных start_span и start_generation.

Включается наличием ключей в .env — без них все функции превращаются в no-op,
и бот работает как раньше. Это важно: трейсинг не должен быть обязательным,
иначе упавший Langfuse потянет за собой бота.

Точка перехвата одна — YandexLlm._call, через который идут все текстовые
вызовы: роутер, планировщик агента, ответы, нутриенты, сводки. Поэтому
достаточно обернуть её, а не расставлять декораторы по всему коду.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any

from app.config import get_settings

log = logging.getLogger(__name__)

_client = None
_checked = False


def _get_client():
    """Ленивая инициализация: клиент нужен не всем процессам (api его не трогает)."""
    global _client, _checked
    if _checked:
        return _client
    _checked = True

    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        log.info("Langfuse выключен: ключи не заданы")
        return None

    try:
        from langfuse import Langfuse

        # base_url, а не host: в SDK v3 параметр host устаревший и молча
        # игнорируется — клиент оставался на localhost:3000, что внутри
        # контейнера бота указывает на сам бот (Connection refused)
        _client = Langfuse(
            public_key=s.langfuse_public_key,
            secret_key=s.langfuse_secret_key,
            base_url=s.langfuse_host,
        )
        log.info("Langfuse включён: %s", s.langfuse_host)
    except Exception:
        log.exception("Langfuse не инициализировался — работаем без трейсинга")
        _client = None
    return _client


def is_enabled() -> bool:
    return _get_client() is not None


@asynccontextmanager
async def trace_request(name: str, user_id: int, text: str, **meta: Any):
    """Один трейс на всё обращение пользователя.

    Без него каждый вызов LLM попадал в Langfuse отдельным корневым трейсом:
    router, agent-plan, agent-answer лежали рядом без связи, и граф обращения
    не собирался. Здесь открывается родительский span, а вложенные генерации
    подхватывают его через контекст OTel автоматически.
    """
    client = _get_client()
    if client is None:
        yield lambda *_a, **_kw: None
        return

    captured: dict[str, Any] = {}
    _graph_step.set(0)

    def finish(output: Any = None, **extra: Any) -> None:
        captured["output"] = output
        captured.update(extra)

    # start_as_current_span делает span активным в контексте OTel — только тогда
    # вложенные generation становятся его детьми, а не корневыми трейсами
    try:
        cm = client.start_as_current_observation(
            as_type="span",
            name=name,
            input={"text": text},
            metadata={"user_id": user_id, **meta},
        )
    except Exception:
        log.exception("Langfuse: не удалось открыть span обращения")
        yield finish
        return

    span = None
    try:
        with cm as span:
            yield finish
    except Exception as e:
        if span is not None:
            try:
                span.update(level="ERROR", status_message=f"{type(e).__name__}: {e}"[:400])
            except Exception:
                log.exception("Langfuse: не удалось пометить span ошибкой")
        raise
    else:
        if span is None:
            return
        try:
            out = captured.pop("output", None)
            span.update(output=out, metadata={"user_id": user_id, **meta, **captured})
            # Вход и выход на самом ТРЕЙСЕ, а не только на спане: иначе в
            # списке трейсов пустые колонки, и непонятно, о чём был разговор.
            # В SDK 4.x это set_trace_io вместо update_trace из 3.x.
            span.set_trace_io(input={"text": text}, output=out)
        except Exception:
            log.exception("Langfuse: не удалось закрыть span обращения")


@asynccontextmanager
async def trace_llm(name: str, model: str, messages: list[dict], **meta: Any):
    """Обёртка вокруг одного вызова модели.

    Отдаёт функцию, которой нужно передать ответ и usage. Ошибки трейсинга
    гасим: сломанный Langfuse не должен ломать ответ пользователю.
    """
    from app import metrics

    # kind для метрик: по имени вызова понятно, это текст, фото или голос
    kind = "vision" if "vision" in name or "photo" in name else (
        "stt" if "stt" in name or "voice" in name else "llm")

    client = _get_client()
    started = time.monotonic()

    if client is None:
        # Langfuse выключен — трейс не пишем, но метрики нужны всё равно:
        # они не зависят от него и должны работать сами по себе
        try:
            yield lambda *_args, **_kw: None
        except Exception:
            metrics.inc("llm_calls", kind=kind, status="error")
            raise
        else:
            metrics.inc("llm_calls", kind=kind, status="ok")
        finally:
            metrics.observe("llm_latency", time.monotonic() - started, kind=kind)
        return

    gen = None
    try:
        gen = client.start_observation(
            as_type="generation",
            name=name,
            model=model,
            input=messages,
            metadata=meta or None,
        )
    except Exception:
        log.exception("Langfuse: не удалось открыть generation")

    captured: dict[str, Any] = {}

    def finish(output: Any = None, usage: dict | None = None) -> None:
        captured["output"] = output
        captured["usage"] = usage

    try:
        yield finish
    except Exception as e:
        metrics.inc("llm_calls", kind=kind, status="error")
        metrics.observe("llm_latency", time.monotonic() - started, kind=kind)
        # ошибку тоже пишем в трейс: по ней потом видно, что упало и на чём
        if gen is not None:
            try:
                gen.update(level="ERROR", status_message=f"{type(e).__name__}: {e}"[:400])
                gen.end()
            except Exception:
                log.exception("Langfuse: не удалось закрыть generation с ошибкой")
        raise
    else:
        metrics.inc("llm_calls", kind=kind, status="ok")
        metrics.observe("llm_latency", time.monotonic() - started, kind=kind)
        if gen is None:
            return
        try:
            usage = captured.get("usage") or {}
            gen.update(
                output=captured.get("output"),
                usage_details={
                    "input": usage.get("prompt_tokens"),
                    "output": usage.get("completion_tokens"),
                    "total": usage.get("total_tokens"),
                } if usage else None,
                metadata={**(meta or {}), "latency_ms": round((time.monotonic() - started) * 1000)},
            )
            gen.end()
        except Exception:
            log.exception("Langfuse: не удалось закрыть generation")


def flush() -> None:
    """Досылает накопленные события. Вызывать при остановке процесса —
    иначе последние трейсы теряются в буфере."""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        log.exception("Langfuse: flush не прошёл")


# счётчик шагов текущего обращения — для вкладки Graph в Langfuse
import contextvars

_graph_step: contextvars.ContextVar[int] = contextvars.ContextVar("graph_step", default=0)


from contextlib import ExitStack


def _langsmith_span(stack: ExitStack, name: str) -> None:
    """Зеркалит шаг в LangSmith: без этого его дерево — безымянные ChatOpenAI.

    Тихо пропускается, если LangSmith не настроен или не установлен.
    """
    import os

    if not (os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")):
        return
    try:
        import langsmith
        stack.enter_context(langsmith.trace(name=name, run_type="chain"))
    except Exception:
        pass


@asynccontextmanager
async def trace_step(name: str, **meta: Any):
    """Шаг графа как span внутри текущего трейса.

    Нужен, чтобы в Langfuse была видна структура графа (router → agent → …),
    а не только плоский список вызовов модели. Метаданные langgraph_node и
    langgraph_step включают в интерфейсе вкладку Graph — Langfuse рисует
    по ним схему сработавших узлов. Шаг зеркалится и в LangSmith.
    """
    client = _get_client()
    with ExitStack() as stack:
        _langsmith_span(stack, name)

        def _noop(input: Any = None, output: Any = None) -> None:
            return

        if client is None:
            yield _noop
            return
        step = _graph_step.get() + 1
        _graph_step.set(step)
        node = name.split(":", 1)[-1]
        try:
            cm = client.start_as_current_observation(
                as_type="span", name=name,
                metadata={"langgraph_node": node, "langgraph_step": step, **meta})
        except Exception:
            log.exception("Langfuse: не удалось открыть span шага %s", name)
            yield _noop
            return
        with cm as span:
            # даём шагу заполнить вход и выход: без них span инструмента
            # виден в графе, но пустой — непонятно, что искали и что нашли
            def fill(input: Any = None, output: Any = None) -> None:
                try:
                    payload: dict[str, Any] = {}
                    if input is not None:
                        payload["input"] = input
                    if output is not None:
                        payload["output"] = output
                    if payload:
                        span.update(**payload)
                except Exception:
                    log.exception("Langfuse: не удалось дописать span %s", name)

            yield fill
