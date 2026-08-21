"""Промпты из Langfuse с фолбэком на локальные.

Зачем: Langfuse становится единой точкой входа — промпт правится в интерфейсе,
и правка применяется без пересборки образа и деплоя. Там же видно историю
версий и какой именно текст ушёл в модель в конкретном трейсе.

Почему с фолбэком, а не только из Langfuse: бот не должен зависеть от
внешнего сервиса ради текста, который у него и так есть. Langfuse лежит,
ключей нет, промпт не создан — берём локальный из prompts.py, и всё работает
как раньше.

Про плейсхолдеры. В коде промпты используют Python-формат `{day}`, а Langfuse
свой — `{{day}}`. Конвертация идёт в обе стороны: при заливке `{x}` -> `{{x}}`,
при чтении обратно. Так один и тот же текст живёт в обоих местах без правок
вызывающего кода — он по-прежнему делает `.format(day=...)`.
"""
from __future__ import annotations

import logging
import re
import time


from app import prompts as local_prompts

log = logging.getLogger(__name__)

# Кеш: промпт меняется редко, а тянуть его по сети на каждый вызов модели —
# лишняя задержка и точка отказа. TTL короткий, чтобы правка в интерфейсе
# применялась в течение минуты, а не после перезапуска.
_CACHE_TTL_SEC = 60
_cache: dict[str, tuple[float, str]] = {}


def _to_langfuse(text: str) -> str:
    """`{day}` -> `{{day}}`. Уже двойные скобки не трогаем."""
    return re.sub(r"(?<!\{)\{(\w+)\}(?!\})", r"{{\1}}", text)


def _to_python(text: str) -> str:
    """`{{day}}` -> `{day}`, обратное преобразование при чтении."""
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", r"{\1}", text)


def get(name: str) -> str:
    """Текст промпта по имени константы из prompts.py.

    Сначала Langfuse, при любой осечке — локальный. Исключения не пробрасываем:
    промпт нужен, чтобы ответить пользователю, и падать из-за трейсинга нельзя.
    """
    local = getattr(local_prompts, f"_RAW_{name}", None)
    if local is None:
        raise AttributeError(f"нет промпта {name} ни в Langfuse, ни в prompts.py")

    return get_or(name, local)


def get_or(name: str, fallback: str) -> str:
    """Как get(), но локальный текст передаётся явно.

    Нужна для prompts.__getattr__: там константы уже переименованы в _RAW_*,
    и get() не нашёл бы их обычным getattr — получилась бы рекурсия.
    """
    now = time.monotonic()
    if (hit := _cache.get(name)) and now - hit[0] < _CACHE_TTL_SEC:
        return hit[1]

    text = fallback
    try:
        from app.tracing import _get_client

        client = _get_client()
        if client is not None:
            fetched = client.get_prompt(name, label="production",
                                        cache_ttl_seconds=_CACHE_TTL_SEC)
            if fetched and fetched.prompt:
                text = _to_python(fetched.prompt)
    except Exception as e:
        log.debug("Langfuse: беру локальный %s (%s)", name, type(e).__name__)

    _cache[name] = (now, text)
    return text


def push_all(overwrite: bool = False) -> dict[str, str]:
    """Заливает все локальные промпты в Langfuse. Возвращает отчёт по каждому.

    Идемпотентна: без overwrite существующие промпты не трогает, чтобы правка
    в интерфейсе не затиралась при повторном запуске.
    """
    from app.tracing import _get_client

    client = _get_client()
    if client is None:
        return {"—": "Langfuse выключен: нет ключей"}

    # _RAW_* — исходные локальные тексты: обычные имена перехватывает
    # prompts.__getattr__ и вернул бы версию из Langfuse, то есть мы бы
    # заливали обратно то, что только что скачали
    names = [
        n[5:] for n in vars(local_prompts)
        if n.startswith("_RAW_") and isinstance(getattr(local_prompts, n), str)
    ]

    report: dict[str, str] = {}
    for name in sorted(names):
        text = getattr(local_prompts, f"_RAW_{name}")

        if not overwrite:
            try:
                if client.get_prompt(name, cache_ttl_seconds=0):
                    report[name] = "уже есть, пропущен"
                    continue
            except Exception:
                pass    # нет промпта — создаём ниже

        try:
            client.create_prompt(
                name=name,
                prompt=_to_langfuse(text),
                labels=["production"],
                # тег по разделу: PHOTO_SYSTEM -> photo, чтобы в интерфейсе
                # промпты группировались, а не лежали плоским списком из 34 штук
                tags=[name.rsplit("_", 1)[0].lower()],
                type="text",
            )
            report[name] = "создан"
        except Exception as e:
            report[name] = f"ошибка: {type(e).__name__}"

    return report
