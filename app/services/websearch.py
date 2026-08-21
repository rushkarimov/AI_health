"""Поиск в интернете: свежие факты и состав редких продуктов.

Провайдер один — DuckDuckGo: без ключа, без лимита и, в отличие от Tavily
и Serper, не блокирует запросы с нашего хостинга (те отдавали 403 с этого
адреса даже без авторизации). Расплата — нестабильность: на один и тот же
запрос он то отдаёт таблицы состава, то случайную выдачу, поэтому пробуем
дважды и отсеиваем результаты не по теме.

Не нашлось ничего — вызывающий код работает на знаниях модели, как раньше.

Результат намеренно короткий: он попадает в промпт, а каждый лишний
абзац — это токены на каждом запросе.
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

TIMEOUT = 12.0

# Сколько выдержек кладём в промпт. Больше пяти модель всё равно не
# использует, а токены тратятся на каждом запросе.
MAX_RESULTS = 5
# Обрезка одной выдержки: полные страницы раздувают промпт впятеро.
SNIPPET_LIMIT = 400


def enabled() -> bool:
    """Поиск доступен всегда: DuckDuckGo работает без ключей."""
    return True


async def _duckduckgo(query: str, count: int) -> list[dict[str, str]]:
    try:
        from ddgs import DDGS
    except ImportError:
        log.info("ddgs не установлен — поиск недоступен")
        return []

    import asyncio

    def _search() -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            return list(ddgs.text(query, region="ru-ru", max_results=count))

    try:
        # ddgs синхронный: без потока он заблокировал бы весь event loop
        rows = await asyncio.to_thread(_search)
    except Exception:
        log.exception("DuckDuckGo не ответил")
        return []

    out = []
    for item in rows[:count]:
        text = (item.get("body") or "").strip()
        if not text:
            continue
        out.append({"title": (item.get("title") or "").strip(),
                    "text": text[:SNIPPET_LIMIT],
                    "url": item.get("href") or ""})
    return out


# DuckDuckGo без ключа иногда отдаёт выдачу не по теме — на «Яшкино крекер»
# приходили страницы Microsoft и китайские биржевые сводки. Отбрасываем
# результаты, в которых нет ни одного слова из запроса: лучше знания модели,
# чем мусор в промпте.
def _relevant(results: list[dict[str, str]], query: str) -> list[dict[str, str]]:
    words = {w.lower() for w in re.findall(r"\w{4,}", query)}
    if not words:
        return results
    out = []
    for r in results:
        blob = f"{r['title']} {r['text']}".lower()
        if any(w in blob for w in words):
            out.append(r)
    return out


async def search(query: str, count: int = MAX_RESULTS) -> list[dict[str, str]]:
    """Выдержки по запросу. Пустой список — значит поиск не помог."""
    results: list[dict[str, str]] = []
    for _ in range(2):
        results = _relevant(await _duckduckgo(query, count), query)
        if results:
            break
    log.info("Поиск %r: %d результатов", query[:60], len(results))
    return results


def as_context(results: list[dict[str, str]], header: str) -> str:
    """Выдержки блоком для промпта. Пустой ввод — пустая строка."""
    if not results:
        return ""
    lines = [header]
    for r in results:
        title = r["title"] or r["url"]
        lines.append(f"- {title}: {r['text']}")
    return "\n".join(lines)


async def food_context(name: str) -> str:
    """Состав продукта из интернета — подсказка к знаниям модели.

    Формулировка «калорийность X» проверена как самая надёжная: она выводит
    таблицы состава (Calorizator и подобные). Длинные запросы вроде
    «…на 100 г белки жиры углеводы» уводят на сайт бренда и Википедию,
    а слово «fatsecret» внутри запроса выдачу только портит.
    """
    results = await search(f"калорийность {name}", count=4)
    return as_context(
        results,
        "Данные из интернета о продукте (могут быть неточными — сверяй "
        "с названием и здравым смыслом):")
