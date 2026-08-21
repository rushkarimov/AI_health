"""Регулярная проверка качества чат-бота: LLM-судья раз в две недели.

Экономия токенов здесь — требование, а не побочная цель:

* всего 3 примера, а не выборка из трафика;
* судья вызывается ОДИН раз на весь прогон (все три пары «вопрос-ответ»
  в одном промпте), а не по разу на пример;
* судья отвечает компактным JSON, без рассуждений вслух;
* часть метрик считается кодом (точность цифр, отказы, длина) — за них
  вообще не платим.

Итого прогон стоит примерно как одно обычное сообщение пользователю.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.db import repo

log = logging.getLogger(__name__)

DATASET_NAME = "health-bot-regress"

# Три вопроса, покрывающие разные умения: работа с данными за период,
# арифметика по балансу и извлечение одного числа за конкретный день.
# Ответы не хардкодим — правду каждый раз берём из базы, иначе тест
# «протухнет» через неделю.
CASES: list[dict[str, str]] = [
    {
        "id": "steps_yesterday",
        "question": "Сколько шагов я прошёл вчера?",
        "metric": "steps",
        "hint": "число шагов за вчерашний день",
    },
    {
        "id": "sleep_last_night",
        "question": "Сколько я спал прошлой ночью?",
        "metric": "sleep_hours",
        "hint": "продолжительность сна в часах",
    },
    {
        "id": "kcal_yesterday",
        "question": "Сколько калорий я съел вчера?",
        "metric": "kcal_eaten",
        "hint": "съеденные калории за вчера",
    },
]

JUDGE_SYSTEM = (
    "Ты — строгий оценщик ответов помощника по здоровью. "
    "Для каждой пары «вопрос — ответ» поставь три оценки от 0 до 1:\n"
    "faithfulness — ответ опирается на приведённые факты и не выдумывает;\n"
    "relevance — отвечает именно на заданный вопрос;\n"
    "coherence — сформулирован ясно и по-русски.\n"
    "Отвечай ТОЛЬКО JSON-массивом без пояснений, по объекту на пример: "
    '[{"id":"...","faithfulness":0.0,"relevance":0.0,"coherence":0.0}]'
)


async def _ground_truth(user_id: int) -> dict[str, float | None]:
    """Правда из базы за вчера — с ней сверяем цифры в ответе."""
    rows = await repo.fetch(
        """
        SELECT steps, sleep_hours, kcal_eaten
        FROM v_daily_full
        WHERE user_id = %s
          AND day = (now() AT TIME ZONE 'Europe/Moscow')::date - 1
        """,
        (user_id,),
    )
    if not rows:
        return {}
    r = rows[0]
    return {k: (float(v) if v is not None else None) for k, v in r.items()}


def _numbers(text: str) -> list[float]:
    """Все числа из ответа — по ним проверяем фактическую точность."""
    out = []
    for m in re.findall(r"\d+(?:[.,]\d+)?", text.replace(" ", "")):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            continue
    return out


def _number_hit(answer: str, truth: float | None) -> float | None:
    """1.0, если верное число прозвучало (допуск 5%), иначе 0.0.

    None — когда правды нет: за отсутствие данных наказывать нельзя,
    такой пример просто не участвует в метрике.
    """
    if truth is None:
        return None
    nums = _numbers(answer)
    if not nums:
        return 0.0
    tol = max(abs(truth) * 0.05, 0.5)
    return 1.0 if any(abs(n - truth) <= tol for n in nums) else 0.0


def _refused(answer: str) -> bool:
    """Ответ-отказ: «данных нет», «не могу» — считаем отдельной метрикой."""
    low = answer.lower()
    return any(p in low for p in ("не могу", "нет данных", "не нашёл",
                                 "не нашел", "не знаю", "недоступ"))


async def run(user_id: int) -> dict[str, Any]:
    """Прогон: задаём 3 вопроса, считаем метрики, шлём оценки в Langfuse."""
    from app import tracing
    from app.graph.agent import ask

    truth = await _ground_truth(user_id)
    results: list[dict[str, Any]] = []

    for case in CASES:
        try:
            answer, _ = await ask(user_id, case["question"])
        except Exception:
            log.exception("Оценка: агент упал на %r", case["id"])
            answer = ""
        results.append({
            "id": case["id"],
            "question": case["question"],
            "answer": answer or "",
            "truth": truth.get(case["metric"]),
            "hint": case["hint"],
        })

    judged = await _judge(results)

    # ---- метрики, которые считает код (бесплатно) ----
    hits = [h for h in (_number_hit(r["answer"], r["truth"]) for r in results)
            if h is not None]
    metrics: dict[str, float] = {}
    if hits:
        metrics["factual_accuracy"] = sum(hits) / len(hits)
    metrics["refusal_rate"] = sum(_refused(r["answer"]) for r in results) / len(results)
    metrics["empty_rate"] = sum(not r["answer"].strip() for r in results) / len(results)
    metrics["answer_len_avg"] = sum(len(r["answer"]) for r in results) / len(results)

    # ---- метрики судьи ----
    for key in ("faithfulness", "relevance", "coherence"):
        vals = [j[key] for j in judged.values() if key in j]
        if vals:
            metrics[key] = sum(vals) / len(vals)

    _push_scores(metrics, results, judged)
    log.info("Оценка качества: %s", {k: round(v, 3) for k, v in metrics.items()})
    return {"metrics": metrics, "results": results}


async def _judge(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Один вызов модели на весь прогон — три примера в одном промпте."""
    from app.providers.factory import get_llm

    blocks = []
    for r in results:
        truth = "нет данных" if r["truth"] is None else r["truth"]
        blocks.append(
            f"id: {r['id']}\nВопрос: {r['question']}\n"
            f"Факт из базы ({r['hint']}): {truth}\n"
            f"Ответ помощника: {r['answer'][:600]}"
        )
    user = "\n\n".join(blocks)

    try:
        raw = await get_llm().complete(JUDGE_SYSTEM, user, json_mode=True)
    except Exception:
        log.exception("Оценка: судья не ответил")
        return {}

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # json_mode иногда заворачивает массив в объект
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return {d["id"]: d for d in data if isinstance(d, dict) and "id" in d}
    except Exception:
        log.warning("Оценка: судья вернул неразбираемый ответ %.200s", raw)
        return {}


def _push_scores(metrics: dict[str, float], results: list[dict[str, Any]],
                 judged: dict[str, dict[str, float]]) -> None:
    """Оценки в Langfuse, привязанные к настоящему трейсу прогона.

    Раньше тут был create_trace_id(): он выдаёт только идентификатор, но
    самого трейса не создаёт — интерфейс показывал «Trace not found» и
    висел в загрузке. Нужен реальный span, внутрь которого кладём и
    вопросы с ответами, и оценки.
    """
    from app import tracing

    client = tracing._get_client()
    if client is None:
        return
    try:
        with client.start_as_current_observation(
            as_type="span",
            name="регрессия качества",
            input={"questions": [r["question"] for r in results]},
            metadata={"examples": len(results)},
        ) as span:
            # Сводка человекочитаемой строкой: в интерфейсе сразу видно
            # вопрос, ответ, правду из базы и вердикт — без раскрытия JSON
            lines = []
            for r in results:
                hit = _number_hit(r["answer"], r["truth"])
                verdict = ("нет правды в базе" if hit is None
                           else ("совпало" if hit else "НЕ СОВПАЛО"))
                lines.append(
                    f"[{r['id']}] {verdict}\n"
                    f"  вопрос:  {r['question']}\n"
                    f"  ответ:   {r['answer'][:200]}\n"
                    f"  правда:  {r['truth']}"
                )
            summary = "\n\n".join(lines)
            span.update(
                output=summary,
                metadata={
                    "examples": len(results),
                    **{f"score.{k}": round(v, 3) for k, v in metrics.items()},
                },
            )
            # Каждый пример — вложенным span'ом: так в дереве трейса видно
            # отдельные вопросы, а не один общий блок
            for r in results:
                hit = _number_hit(r["answer"], r["truth"])
                with client.start_as_current_observation(
                    as_type="span",
                    name=r["id"],
                    input=r["question"],
                    metadata={"правильный_ответ": r["truth"],
                              "цифра_совпала": hit},
                ) as leaf:
                    leaf.update(output=r["answer"][:300])
            for name, value in metrics.items():
                client.score_current_trace(name=name, value=float(value),
                                           data_type="NUMERIC")
            for r in results:
                j = judged.get(r["id"], {})
                hit = _number_hit(r["answer"], r["truth"])
                if hit is not None:
                    client.score_current_trace(name=f"{r['id']}.factual",
                                               value=hit, data_type="NUMERIC")
                for key in ("faithfulness", "relevance", "coherence"):
                    if key in j:
                        client.score_current_trace(
                            name=f"{r['id']}.{key}", value=float(j[key]),
                            data_type="NUMERIC")
        client.flush()
    except Exception:
        log.exception("Оценка: не удалось отправить метрики в Langfuse")
