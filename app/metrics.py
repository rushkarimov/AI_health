"""Метрики для Prometheus и Grafana.

Зачем отдельно от tracing.py: Langfuse отвечает на вопрос «что именно
произошло в этом обращении» (трейс с промптами и ответами), а метрики — на
«сколько и как быстро» в целом. Первое нужно при разборе конкретного случая,
второе — чтобы заметить деградацию до жалоб.

Работает без внешних зависимостей: если prometheus_client не установлен, все
функции превращаются в no-op и бот работает как раньше. То же, что у трейсинга —
мониторинг не должен быть обязательным.

Метрики отдаёт /metrics на api-контейнере: у бота нет HTTP-сервера, а поднимать
второй ради метрик незачем. Бот и api пишут в общий файловый каталог
(PROMETHEUS_MULTIPROC_DIR), поэтому счётчики из обоих процессов видны вместе.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager

log = logging.getLogger(__name__)

_enabled = False
_m: dict = {}


def _init() -> None:
    """Создаёт метрики один раз. Без prometheus_client — тихо выключается."""
    global _enabled, _m
    if _m:
        return

    try:
        from prometheus_client import Counter, Gauge, Histogram
    except ImportError:
        log.info("Метрики выключены: prometheus_client не установлен")
        return

    # Бакеты под реальные времена: вызов LLM 1-10 сек, распознавание фото до 30.
    # Дефолтные бакеты prometheus заканчиваются на 10 сек, и всё медленное
    # сваливалось в +Inf — по такой гистограмме не видно, 12 секунд или 60.
    LATENCY = (0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60)

    _m = {
        # ------------------------------------------------- продуктовые
        "updates": Counter(
            "bot_updates_total", "Сообщений от пользователей",
            ["kind"],          # photo | voice | text | callback
        ),
        "meals": Counter(
            "bot_meals_saved_total", "Записей о еде сохранено",
            ["source"],        # photo | voice | text | combined | manual
        ),
        "sections": Counter(
            "bot_section_opened_total", "Открытий разделов",
            ["section"],       # today | health | labs | psy | advice | last
        ),
        "health_pushes": Counter(
            "bot_health_pushes_total", "Выгрузок метрик из Apple Health",
            ["result"],        # saved | empty | rejected
        ),
        "google_syncs": Counter(
            "bot_google_health_syncs_total", "Синхронизаций с Google Health",
            ["result"],        # saved | empty | error
        ),
        # ------------------------------------------------- технические
        "llm_calls": Counter(
            "bot_llm_calls_total", "Вызовов моделей",
            ["kind", "status"],   # kind: llm|vision|stt, status: ok|error
        ),
        "llm_latency": Histogram(
            "bot_llm_latency_seconds", "Время ответа модели",
            ["kind"], buckets=LATENCY,
        ),
        "handler_latency": Histogram(
            "bot_handler_latency_seconds", "Время обработки обращения",
            ["kind"], buckets=LATENCY,
        ),
        "errors": Counter(
            "bot_errors_total", "Ошибок по месту возникновения",
            ["where"],
        ),
        "db_latency": Histogram(
            "bot_db_latency_seconds", "Время запросов к Postgres",
            buckets=(0.01, 0.05, 0.1, 0.3, 1, 3),
        ),
        # ------------------------------------------------- состояние
        # multiprocess_mode обязателен в multiproc-режиме: без него Gauge
        # падает при сборе, потому что prometheus_client не знает, как свести
        # значения из разных процессов. "max" — берём наибольшее: и бот, и api
        # видят одну и ту же базу, расхождение означает отставание одного из них.
        "users": Gauge("bot_users_total", "Зарегистрированных пользователей",
                       multiprocess_mode="max"),
        "health_lag_days": Gauge(
            "bot_health_lag_days", "Дней с последней выгрузки Apple Health",
            ["user_id"], multiprocess_mode="max",
        ),
        # Бэкапы. Дамп на сервере снимает cron, копию на ноутбук забирает
        # launchd — Prometheus живёт на сервере и до ноутбука не дотянется,
        # поэтому Mac сам сообщает об удачном заборе через /backup/report.
        "backups": Counter(
            "bot_backups_total", "Снятых и забранных резервных копий",
            ["place"],         # server | laptop
        ),
        "backup_age_hours": Gauge(
            "bot_backup_age_hours", "Часов с последней резервной копии",
            ["place"], multiprocess_mode="max",
        ),
        "backup_size_mb": Gauge(
            "bot_backup_size_mb", "Размер последней резервной копии, МБ",
            ["place"], multiprocess_mode="max",
        ),
        "backup_copies": Gauge(
            "bot_backup_copies", "Сколько копий хранится",
            ["place"], multiprocess_mode="max",
        ),
    }
    _enabled = True
    log.info("Метрики включены: %s счётчиков", len(_m))


def inc(name: str, **labels) -> None:
    """Увеличивает счётчик. Имя неизвестно — молча пропускаем: метрика не
    должна ломать логику, ради которой её поставили."""
    _init()
    if not _enabled:
        return
    try:
        metric = _m.get(name)
        if metric is not None:
            (metric.labels(**labels) if labels else metric).inc()
    except Exception:
        log.exception("Не удалось записать метрику %s", name)


def observe(name: str, value: float, **labels) -> None:
    """Пишет значение в гистограмму или gauge."""
    _init()
    if not _enabled:
        return
    try:
        metric = _m.get(name)
        if metric is None:
            return
        metric = metric.labels(**labels) if labels else metric
        if hasattr(metric, "observe"):
            metric.observe(value)
        else:
            metric.set(value)
    except Exception:
        log.exception("Не удалось записать метрику %s", name)


@contextmanager
def timer(name: str, **labels):
    """Измеряет время блока и пишет в гистограмму.

    Синхронный контекст-менеджер: подходит и для async-кода, потому что внутри
    только замер времени, без await.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        observe(name, time.monotonic() - started, **labels)


def warmup() -> None:
    """Создаёт метрики при старте процесса.

    Без этого счётчик появляется только после первого события, и на пустом
    /metrics не видно даже нулей — Grafana показывала бы «No data», хотя всё
    работает. Ставим нули по основным лейблам, чтобы графики начинались от нуля,
    а не от первого события.
    """
    _init()
    if not _enabled:
        return

    for kind in ("photo", "voice", "text", "callback"):
        inc_zero("updates", kind=kind)
    for kind in ("llm", "vision", "stt"):
        for status in ("ok", "error"):
            inc_zero("llm_calls", kind=kind, status=status)
    # Бэкапы: без нулей панели показывали бы «No data» до первого дампа,
    # а это неотличимо от «копии не снимаются».
    # Только счётчик: Gauge здесь обнулять НЕЛЬЗЯ — у них последнее значение
    # и есть показание (сколько копий, какой возраст). Обнуление при каждом
    # старте стирало бы реальные цифры до следующего отчёта, а он раз в сутки.
    for place in ("server", "laptop"):
        inc_zero("backups", place=place)
    for kind in ("photo", "voice", "text"):
        inc_zero("meals", source=kind)
    for result in ("saved", "empty"):
        inc_zero("health_pushes", result=result)
        inc_zero("google_syncs", result=result)


def inc_zero(name: str, **labels) -> None:
    """Регистрирует метрику с нулём: создаёт серию, не меняя значение."""
    _init()
    if not _enabled:
        return
    try:
        metric = _m.get(name)
        if metric is not None and labels:
            metric.labels(**labels).inc(0)
    except Exception:
        log.exception("Не удалось создать метрику %s", name)


def setup_multiproc() -> None:
    """Готовит каталог для метрик из нескольких процессов.

    Бот и api — разные контейнеры, но пишут в один смонтированный каталог.
    Без этого /metrics в api показывал бы только свои счётчики, а вызовы
    моделей (они идут в контейнере бота) не попадали бы в графики вовсе.
    """
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not path:
        return
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        log.exception("Не удалось создать каталог метрик %s", path)
