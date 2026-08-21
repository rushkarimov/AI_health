"""FastAPI-приёмник данных из Apple Health.

Данные из HealthKit нельзя получить по API снаружи — только само приложение
на iPhone имеет к ним доступ. Поэтому телефон сам присылает их сюда:
  * «Быстрые команды» (Shortcuts) — бесплатно, автоматизация по расписанию;
  * Health Auto Export — платно, но настраивается один раз и работает надёжнее.

Формат намеренно терпимый: принимаем и «плоский» JSON от Shortcuts,
и структуру Health Auto Export.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.db import repo
from app.db.pool import close_pool, run_migrations
from app.services import fitbit

# uvicorn настраивает свои логгеры, но не наш: без basicConfig все log.info()
# из этого модуля молча терялись, и отладка выгрузок шла вслепую
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app import metrics

    metrics.setup_multiproc()
    metrics.warmup()

    await run_migrations()

    # прогрев моделей базы знаний в фоне: первый вызов PyTorch стоит ~15 с,
    # и без прогрева эту паузу ловил первый же вопрос пользователя
    import asyncio

    from app.services import knowledge

    asyncio.get_running_loop().run_in_executor(None, knowledge.warmup)

    yield
    await close_pool()


app = FastAPI(title="Health Bot API", lifespan=lifespan)


async def check_token(request: Request, x_api_token: str = Header(default="")) -> None:
    expected = get_settings().health_api_token
    if not expected or x_api_token != expected:
        # диагностика настройки на телефоне: сам токен не пишем в лог, только
        # признаки расхождения — иначе секрет утечёт в логи
        log.warning(
            "401: заголовок X-API-Token %s (длина %s, ожидается %s); "
            "переданные заголовки: %s",
            "отсутствует" if not x_api_token else "не совпал",
            len(x_api_token),
            len(expected),
            sorted(k for k in request.headers if k.lower().startswith(("x-", "auth"))),
        )
        raise HTTPException(status_code=401, detail="bad token")


class HealthPayload(BaseModel):
    """Плоский формат — то, что удобно собрать в «Быстрых командах»."""

    @field_validator("*", mode="before")
    @classmethod
    def _ru_decimal(cls, v):
        """«Быстрые команды» на русской локали отдают 7,5 вместо 7.5, и запрос
        падал с 422. Пустую строку тоже приводим к None: Shortcuts присылает ""
        для метрики, которой за день не было."""
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            if "," in v and v.replace(",", ".", 1).replace(".", "", 1).replace("-", "", 1).isdigit():
                return v.replace(",", ".", 1)
        return v

    user_id: int
    day: date | None = None
    # steps и resting_hr в базе целые, но приходят дробными: «Быстрые команды»
    # присылают сумму 505.0 и среднее 69.01. Строгий int отклонял ВЕСЬ payload —
    # из-за одного пульса терялись и шаги, и километры. Округляем сами.
    steps: int | None = None
    active_kcal: float | None = None
    resting_kcal: float | None = None
    distance_km: float | None = None
    sleep_hours: float | None = None
    resting_hr: int | None = None
    weight_kg: float | None = None
    # с умных весов
    body_fat_pct: float | None = None
    # питание из Apple Health — независимый от meals источник
    dietary_kcal: float | None = None
    # Средний пульс за день — отдельно от resting_hr: тот показывает
    # восстановление, средний — нагрузку дня.
    heart_rate_avg: float | None = None
    # ВСР — главный маркер стресса, нужен «Психологу»
    hrv_ms: float | None = None
    bmi: float | None = None

    @field_validator("steps", "resting_hr", mode="before")
    @classmethod
    def _round_to_int(cls, v):
        """Целые поля из дробных значений.

        Shortcuts присылают сумму «505.0» и среднее «69.01», а колонки в базе
        целые. Строгий int отклонял ВЕСЬ payload — из-за одной десятой пульса
        терялись и шаги, и километры.
        """
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
        try:
            return round(float(v))
        except (TypeError, ValueError):
            return v      # пусть pydantic сам сообщит, что это не число


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/health/echo", dependencies=[Depends(check_token)])
async def echo(request: Request) -> dict[str, Any]:
    """Отладка автоматизации на телефоне: возвращает то, что прислали, и ничего
    не пишет в базу.

    «Быстрые команды» умеют молча отправить пустоту или строку вместо числа,
    и по ответу /health/daily этого не видно — он просто скажет ok. Здесь видно
    сырое тело, разобранный JSON и какие поля мы бы распознали.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as e:
        log.warning("Echo: не JSON (%s): %.200s", e, raw)
        return {"ok": False, "error": f"не JSON: {e}", "raw": raw[:500]}

    known, unknown = {}, {}
    if isinstance(parsed, dict):
        fields = set(HealthPayload.model_fields)
        for k, v in parsed.items():
            (known if k in fields else unknown)[k] = v

    log.info("Echo: %.300s", raw)
    return {
        "ok": True,
        "raw_length": len(raw),
        "parsed": parsed,
        "recognized": known,
        "ignored": unknown,
        "hint": (
            "Пусто в recognized — телефон присылает не те имена полей. "
            "Сверь их со списком в /docs."
        ) if not known else "Поля распознаны, можно бить в /health/daily",
    }


@app.post("/health/daily", dependencies=[Depends(check_token)])
async def push_daily(payload: HealthPayload) -> dict[str, Any]:
    from datetime import datetime

    day = payload.day or date.today()
    values = payload.model_dump(exclude={"user_id", "day"})

    # Ночной пуш (до 05:00) ловит в окно «последний 1 день» ВЧЕРАШНЮЮ ночь
    # и записывал её сон в новый день («сплю 10 часов», хотя ещё не ложился).
    # Та ночь уже записана вчерашним вечерним пушем — сон просто выкидываем.
    if payload.day is None and values.get("sleep_hours") is not None \
            and datetime.now().hour < 5:
        log.info("Ночной пуш: сон %s ч отброшен — эта ночь уже записана вчера",
                 values["sleep_hours"])
        values["sleep_hours"] = None
    # см. комментарий в auto_export: доля vs проценты
    if values.get("body_fat_pct") is not None and values["body_fat_pct"] <= 1:
        values["body_fat_pct"] *= 100
    # приоритет источников (google > app > shortcuts) — в upsert_health_day
    await repo.upsert_health_day(payload.user_id, day, values, source="shortcuts")
    saved = {k: v for k, v in values.items() if v is not None}
    log.info("Health %s за %s: %s", payload.user_id, day, saved)

    from app import metrics

    metrics.inc("health_pushes", result="saved" if saved else "empty")
    return {"ok": True, "day": str(day), "saved": list(saved)}


def _first_number(value: str) -> float | None:
    """Вытаскивает первое число из строки.

    «Найти данные Здоровья» в Shortcuts отдаёт не число, а человекочитаемую
    строку: «94,5 кг», «7 ч 20 мин», «120 шагов, 340 шагов». Пробелы в таких
    значениях ломают URL на уровне HTTP (uvicorn отвечает 400 и закрывает
    соединение), поэтому берём первое число и игнорируем остальное.

    Для списков это тоже разумно: если группирование не настроено и пришло
    «120, 340, 89», первое значение лучше, чем отказ целиком.
    """
    import re

    match = re.search(r"-?\d+(?:[.,]\d+)?", value.replace("\u00a0", " "))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


# Синонимы имён метрик для /health/text. Человек пишет их руками в «Командах»,
# и подстраиваться должен код, а не человек: заставлять помнить точное
# resting_kcal вместо понятного energy_passive — плохая идея.
TEXT_ALIASES = {
    # калории
    "energy_active": "active_kcal", "energy_aktive": "active_kcal",
    "active_energy": "active_kcal", "aktive": "active_kcal",
    "energy_passive": "resting_kcal", "passive_energy": "resting_kcal",
    "basal_energy": "resting_kcal", "passive": "resting_kcal",
    "kcal_burned": "active_kcal",
    # состав тела
    "jir_percent": "body_fat_pct", "jir": "body_fat_pct",
    "fat_percent": "body_fat_pct", "body_fat": "body_fat_pct",
    "ves": "weight_kg", "weight": "weight_kg",
    # пульс и дыхание
    "puls_pokoa": "resting_hr", "puls_pokoy": "resting_hr",
    "pulse_rest": "resting_hr", "puls": "resting_hr", "pulse": "resting_hr",
    "puls_sredniy": "heart_rate_avg", "pulse_avg": "heart_rate_avg",
    "heart_rate": "heart_rate_avg", "serdcebienie": "heart_rate_avg",
    "puls_day": "heart_rate_avg", "puls_avg": "heart_rate_avg",
    "hrv": "hrv_ms", "vsr": "hrv_ms",
    "puls_variable": "hrv_ms", "pulse_variable": "hrv_ms",
    "puls_variability": "hrv_ms", "variability": "hrv_ms",
    # активность
    "shagi": "steps", "step_count": "steps",
    # food — съеденные калории по данным Health, а не шаги: сначала я ошибочно
    # свёл «Foot» к steps, и калории уходили в шаги
    "food": "dietary_kcal", "eda": "dietary_kcal", "kcal_eaten": "dietary_kcal",
    "imt": "bmi", "bmi_index": "bmi", "body_mass_index": "bmi",
    "distance": "distance_km", "km": "distance_km",
    "distanciya": "distance_km",
    # сон
    "sleep": "sleep_hours", "son": "sleep_hours",
    # еда из Health
    "dietary_energy": "dietary_kcal",
}


@app.post("/health/text", dependencies=[Depends(check_token)])
async def push_text(request: Request, user_id: int) -> dict[str, Any]:
    """Приём метрик простым текстом — самый устойчивый способ для Shortcuts.

    Зачем понадобился: «Вычислить статистику» отдаёт значение вместе с
    единицами — «92,44 кг», «7 ч 20 мин». Пробел в URL ломает запрос на уровне
    HTTP (uvicorn отвечает 400 и закрывает соединение), и до кода дело не
    доходит. В теле запроса пробелы безопасны.

    Формат — одна метрика на строку, «имя: значение». Регистр и лишние слова
    не важны, число вытаскиваем сами:

        steps: 6234
        weight_kg: 92,44 кг
        sleep_hours: 7 ч 20 мин
    """
    body = (await request.body()).decode("utf-8", "replace")
    # логируем сырое тело: без этого не видно, что реально присылают Shortcuts,
    # и приходится гадать по кодам ответа
    # 300 символов не хватало: список фаз сна занимает больше, и по логу нельзя
    # было понять, сколько их пришло и почему сон не посчитался
    log.info("health/text от %s, тело (%s байт): %r", user_id, len(body), body[:2000])
    known = set(HealthPayload.model_fields) - {"user_id", "day"}

    # Строки без «имя:» — продолжение предыдущей метрики. Shortcuts отдают
    # список значений («Вычислить статистику» не применили или не сработало),
    # и каждое встаёт на новую строку. Собираем их в один набор и усредняем
    # сами: лучше дать разумное число, чем отказать целиком.
    groups: dict[str, list[float]] = {}
    skipped: list[str] = []
    unknown: list[str] = []
    current: str | None = None

    # Фазы сна: «Найти данные Здоровья» с типом Sleep отдаёт НАЗВАНИЯ фаз
    # (Awake, Deep, Core, Asleep, REM), а не часы — числа в них нет вообще,
    # и sleep_hours приходил пустым. Считаем по числу записей: HealthKit пишет
    # фазы интервалами, у Apple Watch это обычно 5-минутные отрезки.
    #
    # Оценка приблизительная: настоящую длительность каждой записи Shortcuts
    # в этом виде не отдаёт. Awake не считаем — это пробуждения внутри сна.
    # Длительность одной записи HealthKit НЕ фиксирована: у одного устройства
    # это 5 минут, у другого 40 секунд — на живых данных 1037 записей за две
    # ночи дали интервал 0.67 мин, а не 5. Подгонять коэффициент под конкретный
    # телефон бессмысленно, поэтому считаем иначе.
    #
    # Считаем СМЕНЫ фазы, а не сами записи: подряд идущие «Deep, Deep, Deep» —
    # это один отрезок глубокого сна, разбитый на служебные записи. Такая
    # оценка устойчива к тому, как часто устройство пишет данные.
    #
    # 12 минут — калибровка по живым данным при фильтре «последний 1 день»:
    # 29 отрезков дали 5.80 ч против 5 ч 46 мин на экране «Здоровья».
    #
    # ВАЖНО: коэффициент привязан к окну выборки. С фильтром «2 дня» в тело
    # попадают обе ночи, отрезков вдвое больше, и сон завышается вдвое — в
    # Быстрых командах для сна должен стоять «в последний 1 день».
    #
    # Точность всё равно приблизительная. Настоящую длительность Shortcuts в
    # этом виде не отдаёт — для точного значения нужен блок «Длительность».
    SEGMENT_MINUTES = 12
    # InBed считаем ОТДЕЛЬНО: когда есть Apple Watch, телефон параллельно
    # пишет «в кровати» на всю ночь, и его отрезки дублируют фазы часов —
    # сон завышался в полтора раза (54 отрезка = 10.8ч при реальных 6:33).
    # InBed идёт в зачёт только если фазовых записей нет вовсе (нет часов).
    ASLEEP_PHASES = {"deep", "core", "rem", "asleep", "light"}
    INBED = "inbed"
    # Фильтр «в последние 2 дня» отдаёт фазы за ДВЕ ночи, поэтому берём только
    # первую: при сортировке «сначала недавние» она идёт в начале, а границей
    # считаем серию Awake — между ночами человек бодрствует долго.
    AWAKE_BREAK = 3
    sleep_segments = 0       # смены фазы, а не отдельные записи
    inbed_segments = 0       # резерв на случай, когда часов нет
    prev_phase: str | None = None
    awake_run = 0
    night_done = False

    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, value = line.partition(":")
            key = name.strip().lower().replace(" ", "_")
            key = TEXT_ALIASES.get(key, key)
            if key not in known:
                # имя не распознали — говорим об этом в ответе, иначе метрика
                # исчезает молча и человек считает, что всё записалось
                unknown.append(name.strip())
                current = None
                continue
            current = key
            groups.setdefault(key, [])
            if (num := _first_number(value)) is not None:
                groups[key].append(num)
            elif key == "sleep_hours" and value.strip().lower() in ASLEEP_PHASES:
                sleep_segments += 1
                prev_phase = value.strip().lower()
                awake_run = 0
            elif key == "sleep_hours" and value.strip().lower() == INBED:
                inbed_segments += 1
                prev_phase = INBED
                awake_run = 0
        elif current:
            if (num := _first_number(line)) is not None:
                groups[current].append(num)
            elif current == "sleep_hours" and not night_done:
                phase = line.lower()
                if phase == INBED:
                    if phase != prev_phase:
                        inbed_segments += 1
                    prev_phase = phase
                    awake_run = 0
                elif phase in ASLEEP_PHASES:
                    # новый отрезок только при СМЕНЕ фазы: подряд идущие
                    # одинаковые записи — один и тот же участок сна
                    if phase != prev_phase:
                        sleep_segments += 1
                    prev_phase = phase
                    awake_run = 0
                elif phase == "awake":
                    awake_run += 1
                    prev_phase = phase
                    # серия пробуждений = ночь закончилась, дальше идёт
                    # предыдущая ночь из того же двухдневного окна
                    if sleep_segments and awake_run >= AWAKE_BREAK:
                        night_done = True

    # фазы превращаем в часы, только если числом сон не пришёл
    # часов нет — единственный источник телефон, тогда InBed и есть сон
    if not sleep_segments and inbed_segments:
        sleep_segments = inbed_segments
    if sleep_segments and not groups.get("sleep_hours"):
        # Больше 12 часов за раз человек не спит: значит в выборку попали две
        # ночи. Отсечь их по серии Awake не выходит — у Apple Watch между
        # ночами бывает всего один Awake, а внутри ночи их столько же.
        # Поэтому делим пополам: это грубо, но ближе к правде, чем 13.6 ч.
        MAX_ONE_NIGHT_H = 12
        hours = round(sleep_segments * SEGMENT_MINUTES / 60, 2)
        if hours > MAX_ONE_NIGHT_H:
            nights = round(hours / 8) or 1      # 8 ч — типичная ночь
            hours = round(hours / nights, 2)
            log.info("Сон: похоже на %s ночи в выборке, делю -> %s ч",
                     nights, hours)
        # 16 часов — верхняя граница правдоподобного сна; выше почти наверняка
        # значит, что в выборку попало больше одной ночи
        if 0 < hours <= 16:
            groups["sleep_hours"] = [hours]
            log.info("Сон собран из %s отрезков: %s ч", sleep_segments, hours)
        else:
            # не молчим: раньше значение просто исчезало, и причину приходилось
            # искать по сырым логам
            log.warning("Сон отброшен: %s отрезков дают %s ч — похоже, в "
                        "выборку попало больше одной ночи", sleep_segments, hours)
            skipped.append("sleep_hours")

    if groups:
        log.info("Разобрано: %s", {k: len(v) for k, v in groups.items()})

    # что суммируется за день, а что усредняется
    SUMMED = {"steps", "active_kcal", "resting_kcal", "distance_km",
              "sleep_hours", "dietary_kcal"}

    clean: dict[str, str] = {}
    for key, values in groups.items():
        if not values:
            skipped.append(key)
            continue
        num = sum(values) if key in SUMMED else sum(values) / len(values)
        if len(values) > 1:
            log.info("%s: получил %s значений, свёл в %s",
                     key, len(values), "сумму" if key in SUMMED else "среднее")
        clean[key] = str(int(round(num))) if key == "steps" else str(round(num, 2))

    if not clean:
        return {
            "ok": False,
            "error": "не нашёл ни одной метрики",
            "empty": skipped,
            "unknown_names": unknown,
            "hint": "каждая строка вида «steps: 6234»; пустые значения "
                    "означают, что переменная не подставилась",
        }

    try:
        payload = HealthPayload(user_id=user_id, **clean)
    except Exception as e:
        log.warning("Не собрал payload из %s: %s", clean, e)
        return {"ok": False, "error": "значения не подошли", "got": clean}

    result = await push_daily(payload)
    if skipped:
        result["empty"] = skipped
    if unknown:
        result["unknown_names"] = unknown
        log.warning("Неизвестные имена метрик: %s", ", ".join(unknown))
    return result


@app.get("/health/push", dependencies=[Depends(check_token)])
async def push_via_query(
    user_id: int,
    steps: str | None = None,
    active_kcal: str | None = None,
    resting_kcal: str | None = None,
    distance_km: str | None = None,
    sleep_hours: str | None = None,
    weight_kg: str | None = None,
    body_fat_pct: str | None = None,
    resting_hr: str | None = None,
    hrv_ms: str | None = None,
    respiratory_rate: str | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Приём метрик через параметры URL — для «Быстрых команд» на iPhone.

    Зачем отдельно от /health/daily: там нужно собрать JSON в действии «Текст»,
    вставляя переменные вручную, и одна пропущенная кавычка ломает запрос.
    Здесь достаточно склеить URL — Shortcuts это умеют одной строкой.

    Все значения принимаем строками: Shortcuts на русской локали отдают «7,5»,
    а пустую метрику — пустой строкой. Разбираем сами, в HealthPayload.
    """
    raw = {
        "steps": steps, "active_kcal": active_kcal, "resting_kcal": resting_kcal,
        "distance_km": distance_km, "sleep_hours": sleep_hours,
        "weight_kg": weight_kg, "body_fat_pct": body_fat_pct,
        "resting_hr": resting_hr, "hrv_ms": hrv_ms,
        "respiratory_rate": respiratory_rate,
    }
    # Отбрасываем нечисловые значения: Shortcuts присылают буквальный «[ВЕС]»,
    # если переменная не подставилась, и Pydantic валился с 500. Лучше принять
    # то, что распознали, и честно сказать про остальное.
    clean: dict[str, str] = {}
    skipped: list[str] = []
    for key, value in raw.items():
        if value in (None, ""):
            continue
        num = _first_number(value)
        if num is None:
            skipped.append(key)
            continue
        # steps в модели int: 120.0 валился ValidationError, а наружу уходил 500
        clean[key] = str(int(round(num))) if key == "steps" else str(num)

    if not clean:
        return {
            "ok": False,
            "error": "не разобрал ни одного значения",
            "skipped": skipped,
            "hint": "в «Командах» вставь переменную из «Найти данные Здоровья», "
                    "а не текст в квадратных скобках",
        }

    # последняя страховка: любое неожиданное значение не должно давать 500 —
    # человек в «Командах» не увидит из него, что исправить
    try:
        payload = HealthPayload(user_id=user_id, day=day or None, **clean)
    except Exception as e:
        log.warning("Не собрал payload из %s: %s", clean, e)
        return {"ok": False, "error": "значения не подошли", "got": clean}

    result = await push_daily(payload)
    if skipped:
        result["skipped"] = skipped
        log.warning("Не разобрал значения метрик: %s", ", ".join(skipped))
    return result


# ------------------------------- Health Auto Export ---------------------------

# Соответствие имён метрик Health Auto Export нашим колонкам
HAE_MAP = {
    "step_count": "steps",
    "active_energy": "active_kcal",
    "basal_energy_burned": "resting_kcal",
    "walking_running_distance": "distance_km",
    "sleep_analysis": "sleep_hours",
    "resting_heart_rate": "resting_hr",
    "weight_body_mass": "weight_kg",
    "body_fat_percentage": "body_fat_pct",
    "dietary_energy": "dietary_kcal",
    "dietary_energy_consumed": "dietary_kcal",
    # метрики для раздела «Психолог»: ВСР — главный маркер стресса,
    # остальное помогает отличить нагрузку от недосыпа
    "heart_rate_variability": "hrv_ms",
    "body_mass_index": "bmi",
    # походка приходит от Apple Watch, но нам не нужна — гасим шум в логах
    "walking_speed": None,
    "walking_double_support_percentage": None,
    "walking_asymmetry_percentage": None,
    "walking_step_length": None,
}


@app.post("/health/auto-export", dependencies=[Depends(check_token)])
async def auto_export(body: dict[str, Any], user_id: int) -> dict[str, Any]:
    """Приёмник формата Health Auto Export: {"data": {"metrics": [...]}}.

    В нём одна метрика содержит массив точек по дням, поэтому раскладываем
    их по датам и апсертим каждый день отдельно.
    """
    metrics = (body.get("data") or {}).get("metrics") or []
    log.info(
        "Health Auto Export прислал %s метрик: %s",
        len(metrics),
        ", ".join(
            f"{m.get('name')}({len(m.get('data') or [])})" for m in metrics
        ) or "пусто",
    )
    by_day: dict[date, dict[str, Any]] = {}

    for metric in metrics:
        name = metric.get("name")
        column = HAE_MAP.get(name)
        if name in HAE_MAP and column is None:
            continue  # метрика известна, но намеренно не храним
        if not column:
            # раньше просто continue: неизвестные метрики исчезали молча, и было
            # непонятно, чего не хватает в выгрузке. Теперь видно в логах, что
            # телефон присылает, но мы не храним — по этому списку и расширяем.
            if metric.get("data"):
                log.info("Метрика не сохранена (нет колонки): %s", name)
            continue
        units = str(metric.get("units") or "").strip().lower()
        points = metric.get("data") or []

        # сон приходит не как qty, а как разбивка по фазам — разбираем отдельно
        if column == "sleep_hours":
            if points:
                log.info("Сон: пример точки = %.400s", json.dumps(points[0], ensure_ascii=False))
            for point in points:
                day = _parse_day(point.get("date"))
                hours = _sleep_hours(point, units)
                if day and hours is not None:
                    by_day.setdefault(day, {})["sleep_hours"] = hours
            continue

        for point in points:
            day = _parse_day(point.get("date"))
            if not day:
                continue
            value = point.get("qty", point.get("Avg"))
            if value is None:
                log.info("Метрика %s: точка без qty/Avg: %.200s", name,
                         json.dumps(point, ensure_ascii=False))
                continue
            value = _convert_units(column, float(value), units)
            by_day.setdefault(day, {})[column] = value

    for day, values in by_day.items():
        await repo.upsert_health_day(user_id, day, values, source="shortcuts")

    log.info(
        "Health Auto Export: %s дней для %s; единицы: %s",
        len(by_day), user_id,
        {m.get("name"): m.get("units") for m in metrics if HAE_MAP.get(m.get("name"))},
    )
    return {"ok": True, "days": len(by_day)}


# Фазы сна в выгрузке Health Auto Export. Суммируем только собственно сон:
# inBed включает время «лёг, но не спал», и с ним цифра завышается на час-два.
_SLEEP_PHASES = ("deep", "core", "rem", "asleep", "light")


def _sleep_hours(point: dict[str, Any], units: str) -> float | None:
    """Часы сна из одной точки.

    Формат менялся между версиями приложения, поэтому проверяем по порядку:
      * v2 с фазами: {"deep": 1.2, "core": 4.1, "rem": 1.3, "inBed": 8.0}
      * плоский:     {"qty": 7.5}
      * старый:      {"asleep": 7.5, "inBed": 8.1}
    """
    phases = sum(
        float(point[k]) for k in _SLEEP_PHASES
        if isinstance(point.get(k), (int, float))
    )
    if phases > 0:
        value = phases
    else:
        raw = point.get("qty", point.get("totalSleep", point.get("inBed")))
        if raw is None:
            return None
        value = float(raw)

    # приложение отдаёт часы, но на некоторых версиях — минуты
    if units in {"min", "minutes", "мин"} or value > 24:
        value = value / 60
    return round(value, 2) if 0 < value <= 24 else None


KJ_PER_KCAL = 4.184
KM_PER_MILE = 1.609344
KG_PER_LB = 0.45359237

# Колонки, которые мы храним в килокалориях
_KCAL_COLUMNS = {"active_kcal", "resting_kcal", "dietary_kcal"}


def _convert_units(column: str, value: float, units: str) -> float:
    """Приводит значение к единицам, в которых мы храним колонку.

    Health Auto Export отдаёт то, что выставлено в системных настройках iPhone:
    энергия может прийти в кДж (тогда активность выглядит как 1915 вместо 458),
    расстояние в милях, вес в фунтах. Единицы приходят в поле units, поэтому
    смотрим на них, а не угадываем по величине.
    """
    if column in _KCAL_COLUMNS and units in {"kj", "kilojoules", "кдж"}:
        return value / KJ_PER_KCAL
    if column == "distance_km" and units in {"mi", "miles", "mile"}:
        return value * KM_PER_MILE
    if column == "weight_kg" and units in {"lb", "lbs", "pounds"}:
        return value * KG_PER_LB
    if column == "sleep_hours" and units in {"min", "minutes", "мин"}:
        return value / 60
    # HealthKit хранит процент жира долей (0.22), весы показывают 22% —
    # приводим к процентам, иначе NUMERIC(4,1) сохранит 0.2
    if column == "body_fat_pct" and value <= 1:
        return value * 100
    return value


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    # формат HAE: "2026-07-30 00:00:00 +0300"
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    log.warning("Не разобрал дату: %r", raw)
    return None


# ------------------------------------------------------- мини-апп регистрации

class RegisterPayload(BaseModel):
    """Тело формы. user_id ЗДЕСЬ НЕТ намеренно: его берём из подписанного
    initData, иначе любой мог бы зарегистрировать чужой аккаунт."""

    init_data: str
    invite_code: str
    name: str = Field(min_length=2, max_length=60)
    sex: str | None = None
    birth_date: date | None = None
    height_cm: float | None = Field(default=None, ge=100, le=250)


async def _greet_after_register(user_id: int, name: str) -> None:
    """Присылает справку и меню сразу после регистрации.

    Своим экземпляром Bot, а не тем, что в контейнере бота: API — отдельный
    процесс, общей шины между ними нет. Один запрос к Telegram дешевле, чем
    поднимать очередь ради одного сообщения в жизни пользователя.

    Ошибки глушим: регистрация уже прошла и в базе всё записано, поэтому
    падать из-за недоставленного приветствия нельзя — человек просто нажмёт
    /start сам.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from app.handlers.commands import HELP
    from app.handlers.keyboards import main_menu_kb, start_grid_kb

    s = get_settings()
    if not s.bot_token:
        return

    bot = Bot(s.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    try:
        # обложка первой, как в /start: новичок иначе увидел бы её только при
        # повторном вызове команды
        from aiogram.types import FSInputFile

        from app.handlers.commands import COVER

        if COVER.exists():
            try:
                await bot.send_photo(user_id, FSInputFile(COVER))
            except Exception:
                log.exception("Не удалось отправить обложку %s", user_id)

        await bot.send_message(
            user_id,
            f"Готово, {name} — доступ открыт 🎉\n\n" + HELP,
            reply_markup=main_menu_kb(),
        )
        await bot.send_message(user_id, "👇 *Разделы*",
                               reply_markup=start_grid_kb())
    except Exception:
        log.exception("Не удалось поздравить %s с регистрацией", user_id)
    finally:
        await bot.session.close()


# ------------------------------------------------- редактор записи о еде


def _clean_meal_items(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Приводит присланное к тому, что ждёт save_meal.

    Форма отдаёт строки и может прислать пустое название — доверять ей нельзя,
    поэтому нормализуем здесь, а не в SQL.
    """
    def f(v, default=0.0) -> float:
        try:
            return float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            return default

    out = []
    for it in raw:
        name = (it.get("resolved_name") or it.get("name") or "").strip()
        grams = f(it.get("grams"))
        if not name or grams <= 0:
            continue
        k = grams / 100.0
        # калории пересчитываем от kcal_100, а не берём присланные: так итог
        # всегда сходится с граммовкой, даже если форма отстала на один ввод
        out.append({
            "name": name,
            "resolved_name": name,
            "grams": round(grams, 1),
            "kcal": round(f(it.get("kcal_100")) * k, 1),
            "protein": round(f(it.get("protein_100")) * k, 1),
            "fat": round(f(it.get("fat_100")) * k, 1),
            "carbs": round(f(it.get("carbs_100")) * k, 1),
            "confidence": it.get("confidence"),
            "food_source": it.get("food_source") or "manual",
        })
    return out


async def _notify_meal_saved(user_id: int, meal_id: int, items: list[dict],
                             total: float, edited: bool) -> None:
    """Сообщение в чат после сохранения из формы.

    Своим экземпляром Bot: API — отдельный процесс от контейнера бота, общей
    шины между ними нет. Ошибки глушим — запись уже в базе.
    """
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    s = get_settings()
    if not s.bot_token:
        return

    lines = [f"• {i['resolved_name']} — {i['grams']:.0f} г · {i['kcal']:.0f} ккал"
             for i in items]
    head = "✏️ Запись обновлена" if edited else "✅ Записано"
    text = f"{head} (#{meal_id})\n\n" + "\n".join(lines) + \
           f"\n\nИтого {total:.0f} ккал"

    bot = Bot(s.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    try:
        await bot.send_message(user_id, text)
    except Exception:
        log.exception("Не удалось сообщить о записи %s", meal_id)
    finally:
        await bot.session.close()


class NutrientsPayload(BaseModel):
    """Запрос состава у модели — кнопка AI в редакторе."""

    init_data: str
    name: str = Field(min_length=2, max_length=120)


# --------------------------------------------- журнал съеденного по датам


async def _notify_profile_updated(user_id: int, name: str) -> None:
    """Короткое подтверждение после правки анкеты — вместо полной справки."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    s = get_settings()
    if not s.bot_token:
        return

    bot = Bot(s.bot_token,
              default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    try:
        await bot.send_message(user_id, f"✅ Анкета обновлена, {name}")
    except Exception:
        log.exception("Не удалось сообщить о правке анкеты %s", user_id)
    finally:
        await bot.session.close()


# --------------------------------------------------------------- метрики

class BackupReport(BaseModel):
    """Отчёт о резервной копии, снятой вне сервера."""

    code: str
    place: str = "laptop"      # где лежит копия: laptop | server
    size_mb: float = 0.0
    copies: int = 0
    age_hours: float = 0.0     # возраст самого свежего дампа


@app.post("/backup/report", include_in_schema=False)
async def backup_report(payload: BackupReport) -> dict[str, str]:
    """Ноутбук сообщает, что забрал копию базы.

    Prometheus живёт на сервере и до ноутбука за NAT не дотянется, а ставить
    ради одной метрики pushgateway на 3.8 ГБ RAM накладно. Поэтому Mac сам
    стучится сюда после каждого удачного забора, а метрики отдаёт уже api.
    """
    import hmac as _hmac

    from app import metrics

    s = get_settings()
    if not s.invite_code or not _hmac.compare_digest(payload.code.strip(),
                                                     s.invite_code):
        raise HTTPException(status_code=403, detail="код не подходит")

    place = payload.place if payload.place in ("laptop", "server") else "laptop"
    metrics.inc("backups", place=place)
    metrics.observe("backup_age_hours", payload.age_hours, place=place)
    metrics.observe("backup_size_mb", payload.size_mb, place=place)
    metrics.observe("backup_copies", payload.copies, place=place)
    log.info("Бэкап (%s): %.1f МБ, копий %d, возраст %.1f ч",
             place, payload.size_mb, payload.copies, payload.age_hours)
    return {"ok": "принято"}


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint() -> Any:
    """Метрики для Prometheus.

    Живут на api-контейнере, а не на боте: у бота нет HTTP-сервера, а поднимать
    второй ради /metrics незачем. Счётчики из процесса бота попадают сюда через
    общий каталог PROMETHEUS_MULTIPROC_DIR.
    """
    from fastapi.responses import PlainTextResponse

    try:
        import os

        from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
        from prometheus_client import multiprocess
    except ImportError:
        return PlainTextResponse("prometheus_client не установлен\n", status_code=503)

    try:
        if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
            registry = CollectorRegistry()
            multiprocess.MultiProcessCollector(registry)
            data = generate_latest(registry)
        else:
            from prometheus_client import REGISTRY

            data = generate_latest(REGISTRY)
    except Exception:
        log.exception("Не удалось собрать метрики")
        return PlainTextResponse("сбор метрик упал\n", status_code=500)

    return PlainTextResponse(data.decode("utf-8"), media_type=CONTENT_TYPE_LATEST)


# ------------------------------------------------- чат для iOS-приложения


try:
    from langsmith import traceable as _ls_traceable
except ImportError:      # LangSmith не установлен — прозрачная заглушка
    def _ls_traceable(*_a, **_kw):
        def deco(fn):
            return fn
        return deco


@_ls_traceable(name="food-router", run_type="chain")
async def _try_parse_food(user_id: int, text: str) -> dict | None:
    """None — сообщение не про еду; иначе распознанные позиции.

    Роутер и разбор — те же промпты, что в боте (ROUTER_*, VOICE_*), чтобы
    «Lays» одинаково понимался и в Телеграме, и в приложении.
    """
    from app import prompts
    from app.providers.factory import get_llm
    from app.providers.parsing import extract_json, parse_recognition_json
    from app.services.resolver import resolve_item

    try:
        raw = await get_llm().complete(
            prompts.ROUTER_SYSTEM,
            prompts.ROUTER_USER.format(text=text),
            json_mode=True,
        )
        if (extract_json(raw) or {}).get("intent") != "food":
            return None

        parsed = await get_llm().complete(
            prompts.VOICE_SYSTEM, prompts.VOICE_USER.format(text=text),
            json_mode=True,
        )
        rec = parse_recognition_json(parsed)
        if not rec.items:
            return None
        items = [await resolve_item(user_id, it) for it in rec.items]
        return {"items": items, "note": rec.note}
    except Exception:
        # роутер сломался — ведём себя как раньше: всё уходит агенту
        log.exception("Чат-приложение: роутер/разбор еды упал")
        return None


async def _table_png_b64(data: dict | None) -> str | None:
    """PNG-таблица ботовским рендером (эмодзи, цвета) в base64.

    None при любой ошибке: клиенты тогда рисуют таблицу из данных сами.
    """
    if not data or not data.get("rows"):
        return None

    import asyncio
    import base64

    from app.services.tables import render_table

    try:
        png = await asyncio.to_thread(
            render_table, data.get("title", ""), data.get("header", []),
            data["rows"], data.get("aligns"), data.get("colors"),
            data.get("footer", ""), data.get("icons"), data.get("head_icons"),
        )
        return base64.b64encode(png).decode()
    except Exception:
        log.exception("Чат-приложение: PNG-таблица не отрисовалась")
        return None


# ------------------------------------------- разделы и фото для чат-клиентов


# Типичное время приёмов: запись должна попадать в СВОЮ группу журнала.
# Окна группировки: завтрак 5-11, обед 11-16, ужин 16-23, перекус — остальное.
# Перекус раньше стоял на 16:30 и проваливался в окно ужина.
_SLOT_TIMES = {"breakfast": "09:00", "lunch": "13:30",
               "snack": "23:30", "dinner": "19:30"}


async def _require_chat_access(code: str, user_id: int) -> None:
    """Общая проверка для всех ручек чат-клиентов.

    Принимаем либо код приглашения (старый вход, Телеграм-мини-приложение),
    либо токен сессии из входа по телефону: у приложения общего секрета нет
    и быть не должно.
    """
    import hmac as _hmac

    s = get_settings()
    token = code.strip()
    if s.invite_code and _hmac.compare_digest(token, s.invite_code):
        pass
    else:
        from app.services import phone_auth
        session_user = await phone_auth.user_by_session(token)
        if session_user is None or session_user != user_id:
            raise HTTPException(status_code=403, detail="код не подходит")
    if not await repo.is_registered(user_id):
        raise HTTPException(status_code=403, detail="пользователь не зарегистрирован")


def _add_recovery(days: list[dict]) -> None:
    """Готовность 0–100, как Recovery в WHOOP: HRV 50% + пульс покоя 25% +
    сон 25%, каждое — против личного базлайна за предыдущие 14 дней.
    Компоненты без данных выпадают, веса перенормируются."""
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    for i, d in enumerate(days):
        hist = days[max(0, i - 14):i]

        def base(key):
            vals = [_f(h.get(key)) for h in hist]
            vals = [v for v in vals if v]
            return sum(vals) / len(vals) if len(vals) >= 3 else None

        parts: list[tuple[float, float]] = []
        hrv, b_hrv = _f(d.get("hrv_ms")), base("hrv_ms")
        if hrv and b_hrv:
            parts.append((0.5, min(hrv / b_hrv, 1.2) / 1.2))
        rhr, b_rhr = _f(d.get("resting_hr")), base("resting_hr")
        if rhr and b_rhr:
            parts.append((0.25, min(b_rhr / rhr, 1.2) / 1.2))
        sleep = _f(d.get("sleep_hours"))
        if sleep:
            parts.append((0.25, min(sleep / 8, 1.0)))
        if parts:
            weight = sum(w for w, _ in parts)
            d["recovery"] = round(100 * sum(w * v for w, v in parts) / weight)
        else:
            d["recovery"] = None


def _add_stress(days: list[dict]) -> None:
    """Стресс 0–100: обратная сторона восстановления.

    Считаем по отклонению от ЛИЧНОГО базлайна за 14 дней, а не от табличных
    норм: у каждого своя физиология, и «нормальный» пульс покоя 48 у одного
    человека означает то же, что 62 у другого.

    Признаки нагрузки: HRV ниже обычного (главный — при стрессе вариабельность
    падает), пульс покоя выше обычного, недосып, дыхание чаще обычного.
    Компоненты без данных выпадают, веса перенормируются — иначе один
    пропущенный показатель обнулял бы всю метрику.
    """
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    for i, d in enumerate(days):
        hist = days[max(0, i - 14):i]

        def base(key):
            vals = [v for v in (_f(h.get(key)) for h in hist) if v]
            return sum(vals) / len(vals) if len(vals) >= 3 else None

        parts: list[tuple[float, float]] = []

        hrv, b_hrv = _f(d.get("hrv_ms")), base("hrv_ms")
        if hrv and b_hrv:
            # HRV упал вдвое от базлайна → максимальный стресс по этому каналу
            drop = max(0.0, min((b_hrv - hrv) / (b_hrv * 0.5), 1.0))
            parts.append((0.45, drop))

        rhr, b_rhr = _f(d.get("resting_hr")), base("resting_hr")
        if rhr and b_rhr:
            # +15% к пульсу покоя — уже заметная нагрузка
            rise = max(0.0, min((rhr - b_rhr) / (b_rhr * 0.15), 1.0))
            parts.append((0.25, rise))

        sleep = _f(d.get("sleep_hours"))
        if sleep:
            # 8 часов — ноль вклада, 4 и меньше — максимум
            parts.append((0.20, max(0.0, min((8 - sleep) / 4, 1.0))))

        br, b_br = _f(d.get("breath_rate")), base("breath_rate")
        if br and b_br:
            rise = max(0.0, min((br - b_br) / (b_br * 0.20), 1.0))
            parts.append((0.10, rise))

        if parts:
            weight = sum(w for w, _ in parts)
            d["stress"] = round(100 * sum(w * v for w, v in parts) / weight)
        else:
            d["stress"] = None


# Норма VO2max по возрасту для мужчин и женщин (мл/кг/мин, средний уровень).
# Источник ориентиров — таблицы Купера: они же лежат в основе «фитнес-возраста»
# у Garmin и Polar. Ключ — верхняя граница возрастной группы.
_VO2_NORM = {
    "m": [(29, 42.5), (39, 41.0), (49, 38.0), (59, 35.0), (69, 31.0), (200, 28.0)],
    "f": [(29, 35.5), (39, 33.5), (49, 31.5), (59, 29.0), (69, 27.0), (200, 25.0)],
}


def _vo2_norm(age: int, sex: str | None) -> float:
    table = _VO2_NORM.get((sex or "m").lower()[:1], _VO2_NORM["m"])
    for upper, val in table:
        if age <= upper:
            return val
    return table[-1][1]


def _add_bio_age(days: list[dict], age: int | None, sex: str | None) -> None:
    """Биологический возраст: паспортный, скорректированный по форме.

    Честная оговорка: настоящий биологический возраст определяют по крови или
    метилированию ДНК. По данным браслета считается «возраст по физической
    форме» — но отсчитываем его от указанного в анкете возраста, поэтому
    величина остаётся привязанной к человеку, а не абстрактной.

    Основа — VO2max: в исследованиях это самый сильный предиктор. Пульс покоя
    и активность уточняют оценку. Максимальная поправка ±12 лет: дальше
    расходится с реальностью даже у спортсменов.
    """
    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if not age or age < 14:
        for d in days:
            d["bio_age"] = None
        return

    norm = _vo2_norm(age, sex)

    for i, d in enumerate(days):
        # VO2max меняется медленно: берём последнее известное за 30 дней,
        # иначе метрика мигала бы прочерками в дни без замера
        vo2 = _f(d.get("vo2max"))
        if vo2 is None:
            for h in reversed(days[max(0, i - 30):i]):
                vo2 = _f(h.get("vo2max"))
                if vo2:
                    break
        if not vo2:
            d["bio_age"] = None
            continue

        # Каждый 1 мл/кг/мин отклонения от нормы ≈ 0.9 года формы
        delta = -(vo2 - norm) * 0.9

        hist = days[max(0, i - 30):i + 1]
        rhr_vals = [v for v in (_f(h.get("resting_hr")) for h in hist) if v]
        if rhr_vals:
            rhr = sum(rhr_vals) / len(rhr_vals)
            # 60 уд/мин — ориентир; каждые 5 ударов ≈ год
            delta += (rhr - 60) / 5.0

        steps_vals = [v for v in (_f(h.get("steps")) for h in hist) if v]
        if steps_vals:
            steps = sum(steps_vals) / len(steps_vals)
            # 8000 шагов — ориентир; ±4000 даёт ±1.5 года
            delta -= max(-1.5, min((steps - 8000) / 4000 * 1.5, 1.5))

        delta = max(-12.0, min(delta, 12.0))
        d["bio_age"] = round(age + delta, 1)


class SleepPayload(BaseModel):
    code: str
    user_id: int
    day: str


_QTY_RE = (r"(\d+(?:\.\d+)?)\s*"
           r"(кг|килограмм\w*|грам\w*|гр\b|г\b|мл\b|миллилитр\w*|"
           r"л\b|литр\w*|шт\b|штук\w*|штуч\w*)")


async def _unit_to_grams(user_id: int, n: float, unit: str,
                         item: dict) -> float | None:
    """Число + единица → граммы. Жидкости 1 мл ≈ 1 г; вес штуки — из каталога."""
    if unit.startswith(("кг", "килограмм", "литр")) or unit == "л":
        return n * 1000
    if unit.startswith(("шт", "штук", "штуч")):
        name = item.get("resolved_name") or item.get("name") or ""
        rows = await repo.fetch(
            """SELECT unit_grams FROM my_foods
               WHERE user_id = %s AND unit = 'шт' AND unit_grams > 0
                 AND (alias = lower(%s) OR canonical_name ILIKE %s
                      OR %s ILIKE '%%' || alias || '%%')
               ORDER BY hits DESC LIMIT 1""",
            (user_id, name, f"%{name}%", name))
        return n * float(rows[0]["unit_grams"]) if rows else None
    return n


async def _grams_by_item(user_id: int, caption: str,
                         items: list[dict]) -> dict[int, float]:
    """Сопоставляет количества из подписи блюдам: {индекс: граммы}.

    Количество привязывается к ближайшему названию блюда (по корню слова
    в пределах ~25 символов). Одно блюдо и одно число — совпадение и так
    однозначное.
    """
    import re

    text = caption.lower().replace(",", ".")
    qtys = [(m.start(), float(m.group(1)), m.group(2))
            for m in re.finditer(_QTY_RE, text)]
    if not qtys:
        return {}

    if len(items) == 1 and len(qtys) == 1:
        grams = await _unit_to_grams(user_id, qtys[0][1], qtys[0][2], items[0])
        return {0: grams} if grams else {}

    # позиция каждого блюда в подписи — по корням слов названия
    result: dict[int, float] = {}
    used: set[int] = set()
    for idx, it in enumerate(items):
        name = (it.get("resolved_name") or it.get("name") or "").lower()
        positions = []
        for word in re.findall(r"[а-яёa-z]{4,}", name):
            stem = word[:5]
            for m in re.finditer(re.escape(stem), text):
                positions.append(m.start())
        if not positions:
            continue
        # ближайшее незанятое количество к любому вхождению названия
        best: tuple[float, int] | None = None
        for qi, (pos, n, unit) in enumerate(qtys):
            if qi in used:
                continue
            dist = min(abs(pos - p) for p in positions)
            if dist <= 25 and (best is None or dist < best[0]):
                best = (dist, qi)
        if best is not None:
            _, qi = best
            grams = await _unit_to_grams(user_id, qtys[qi][1], qtys[qi][2], it)
            if grams:
                result[idx] = grams
                used.add(qi)
    return result


async def _personalize_item(user_id: int, it: dict) -> dict:
    """Персональный сканер: узнаёт ТВОИ бренды и привычные порции.

    Если распознанное блюдо есть в каталоге и ты его уже ел (hits > 0) —
    подставляем твоё название («Чипсы Lay's сырные»), твои ккал/100 и
    привычную граммовку вместо усреднённой оценки модели.
    """
    try:
        name = it.get("resolved_name") or it.get("name") or ""
        rows = await repo.fetch(
            """
            SELECT canonical_name, kcal_100, protein_100, fat_100, carbs_100,
                   default_grams, hits
            FROM my_foods
            WHERE user_id = %s AND hits > 0
              AND (alias = lower(%s) OR alias = lower(%s)
                   OR canonical_name ILIKE %s OR %s ILIKE '%%' || alias || '%%')
            ORDER BY hits DESC
            LIMIT 1
            """,
            (user_id, name, it.get("name") or "", f"%{name}%", name))
        if not rows:
            return it
        r = rows[0]
        grams = float(r["default_grams"] or it.get("grams") or 100)
        k = grams / 100.0
        it.update({
            "resolved_name": r["canonical_name"],
            "grams": grams,
            "kcal_100": float(r["kcal_100"]),
            "protein_100": float(r["protein_100"]),
            "fat_100": float(r["fat_100"]),
            "carbs_100": float(r["carbs_100"]),
            "kcal": round(float(r["kcal_100"]) * k),
            "protein": round(float(r["protein_100"]) * k, 1),
            "fat": round(float(r["fat_100"]) * k, 1),
            "carbs": round(float(r["carbs_100"]) * k, 1),
            "food_source": "personal",
        })
        log.info("Персональный сканер: %r → %r (%s г, hits=%s)",
                 name, r["canonical_name"], grams, r["hits"])
    except Exception:
        log.exception("Персонализация позиции не удалась")
    return it


# ------------------------------------------- чат-приложение: вкладки-разделы
# Дублируют функции webapp/* и разделов бота, но с авторизацией code+user_id:
# веб-чат и iOS-приложение живут вне Телеграма, init_data у них нет.


class SyncSourcePayload(BaseModel):
    code: str
    user_id: int
    sources: list[dict] | None = None   # [{source, enabled, rank}]


class WeightPayload(BaseModel):
    code: str
    user_id: int
    weight_kg: float
    day: str | None = None


class AppHealthPayload(BaseModel):
    code: str
    user_id: int
    days: list[dict]        # [{day: "YYYY-MM-DD", steps: …, sleep_hours: …}]


class UiPrefsPayload(BaseModel):
    code: str
    user_id: int
    prefs: dict | None = None


def _lab_flag(value: float | None, low: float | None, high: float | None) -> str | None:
    if value is None:
        return None
    if low is not None and value < low:
        return "low"
    if high is not None and value > high:
        return "high"
    return "normal"


async def _advice_messages(user_id: int) -> tuple[str, str] | str:
    """(system, user) для рекомендации или строка-заметка, если данных нет."""
    from app import prompts
    from app.handlers.commands import ADVICE_DAYS, _fmt_averages

    nutrition = await repo.fetch(
        """
        SELECT day, round(eaten_kcal) AS kcal, round(protein) AS protein,
               round(fat) AS fat, round(carbs) AS carbs, meals_count
        FROM v_daily_nutrition
        WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
        ORDER BY day DESC
        """,
        (user_id, ADVICE_DAYS),
    )
    health = await repo.fetch(
        """
        SELECT day, steps, round(active_kcal) AS active, round(total_kcal) AS total,
               sleep_hours, resting_hr, weight_kg, body_fat_pct
        FROM health_daily
        WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
        ORDER BY day DESC
        """,
        (user_id, ADVICE_DAYS),
    )
    if not nutrition and not health:
        return "Пока нечего разбирать — нет записей о еде и данных Health."

    def table(rows: list[dict]) -> str:
        if not rows:
            return "нет данных"
        out = []
        for r in rows:
            parts = [f"{r['day']:%d.%m}"]
            parts += [f"{k}={v}" for k, v in r.items() if k != "day" and v is not None]
            out.append(" ".join(parts))
        return "\n".join(out)

    goals = await repo.get_active_goals(user_id)
    facts = await repo.get_facts(user_id)
    avg = await repo.averages(user_id, ADVICE_DAYS)

    return (prompts.ADVICE_SYSTEM,
            prompts.ADVICE_USER.format(
                days=ADVICE_DAYS,
                nutrition=table(nutrition),
                health=table(health),
                averages=_fmt_averages(avg),
                goal=goals[0]["goal_text"] if goals else "не задана",
                facts=", ".join(facts) if facts else "ничего",
            ))


async def _health_advice_messages(user_id: int) -> tuple[str, str] | str:
    """(system, user) для рекомендаций по здоровью — метрики без цели."""
    from app import prompts
    from app.handlers.commands import ADVICE_DAYS, _fmt_averages

    health = await repo.fetch(
        """
        SELECT day, steps, round(active_kcal) AS active, round(total_kcal) AS total,
               sleep_hours, resting_hr, hrv_ms, breath_rate, spo2_pct,
               heart_rate_avg
        FROM health_daily
        WHERE user_id = %s AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
        ORDER BY day DESC
        """,
        (user_id, ADVICE_DAYS),
    )
    if not health:
        return "Пока нечего разбирать — метрики ещё не приехали."

    rows = []
    for r in health:
        parts = [f"{r['day']:%d.%m}"]
        parts += [f"{k}={v}" for k, v in r.items() if k != "day" and v is not None]
        rows.append(" ".join(parts))
    avg = await repo.averages(user_id, ADVICE_DAYS)
    return (prompts.HEALTH_ADVICE_SYSTEM,
            prompts.HEALTH_ADVICE_USER.format(
                days=ADVICE_DAYS,
                health="\n".join(rows),
                averages=_fmt_averages(avg),
            ))


async def _psy_messages(user_id: int) -> tuple[str, str] | str:
    from app import prompts
    from app.handlers.psy import PSY_DAYS

    rows = await repo.fetch(
        """
        SELECT day, sleep_hours, hrv_ms, resting_hr, heart_rate_avg, steps,
               round(kcal_eaten) AS eaten, round(kcal_burned) AS burned
        FROM v_daily_full
        WHERE user_id = %s
          AND day >= (now() AT TIME ZONE 'Europe/Moscow')::date - %s
        ORDER BY day DESC
        """,
        (user_id, PSY_DAYS - 1),
    )
    if not rows:
        return "Нет данных для разбора. Нужны хотя бы сон и пульс из Apple Health."

    def fmt(cols: list[str]) -> str:
        out = []
        for r in rows:
            vals = [f"{k}={r[k]}" for k in cols if r.get(k) is not None]
            if vals:
                out.append(f"{r['day']:%d.%m} " + " ".join(vals))
        return "\n".join(out) or "нет данных"

    facts = await repo.get_facts(user_id)
    goals = await repo.get_active_goals(user_id)
    if goals:
        facts = [f"цель: {goals[0]['goal_text']}"] + list(facts)

    return (prompts.PSY_SYSTEM,
            prompts.PSY_USER.format(
                days=PSY_DAYS,
                sleep=fmt(["sleep_hours"]),
                heart=fmt(["hrv_ms", "resting_hr", "heart_rate_avg"]),
                activity=fmt(["steps", "eaten", "burned"]),
                facts=", ".join(facts) if facts else "ничего",
                note="",
            ))


async def _labs_explain_messages(user_id: int) -> tuple[str, str] | str:
    from app import prompts

    rows = await repo.last_labs(user_id)
    if not rows:
        return "Сначала добавь анализы — фото бланка или текстом."

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
        " ".join([f"{r['day']:%d.%m}"]
                 + [f"{k}={v}" for k, v in r.items() if k != "day" and v is not None])
        for r in ctx
    ) or "нет данных"

    goals = await repo.get_active_goals(user_id)
    return (prompts.LABS_ADVICE_SYSTEM,
            prompts.LABS_ADVICE_USER.format(
                taken_on=f"{rows[0]['taken_on']:%d.%m.%Y}",
                labs=labs_text,
                context=context,
                goal=goals[0]["goal_text"] if goals else "не задана",
            ))


class PhoneStartPayload(BaseModel):
    phone: str = Field(min_length=5, max_length=25)


class PhoneVerifyPayload(BaseModel):
    token: str
    code: str = Field(min_length=4, max_length=8)


@app.post("/auth/phone/start")
async def auth_phone_start(payload: PhoneStartPayload) -> dict[str, Any]:
    """Шаг 1: приложение отдаёт номер, получает ссылку на бота.

    Кода приглашения здесь нет намеренно: подлинность подтверждает сам
    Телеграм, когда пользователь делится номером внутри бота.
    """
    from app.services import phone_auth

    res = await phone_auth.start_auth(payload.phone)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error", "ошибка"))

    # Знакомый номер — бот пишет код сразу, шаг с «Поделиться» не нужен
    if res.get("known"):
        got = await phone_auth.code_for_known(payload.phone, res["token"])
        if got.get("ok"):
            try:
                from aiogram import Bot
                from aiogram.client.default import DefaultBotProperties
                from aiogram.enums import ParseMode

                bot = Bot(get_settings().bot_token,
                          default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                try:
                    await bot.send_message(
                        got["user_id"],
                        "Код для входа в приложение. Действует 10 минут. "
                        "Если вход запрашивал не ты — просто не вводи его.")
                    # код отдельным сообщением: так его удобно копировать
                    await bot.send_message(
                        got["user_id"], f"<code>{got['code']}</code>",
                        parse_mode="HTML")
                finally:
                    await bot.session.close()
                return {"token": res["token"], "sent": True, "link": res["link"]}
            except Exception:
                log.exception("Не удалось отправить код в Телеграм")

    return {"token": res["token"], "sent": False, "link": res["link"]}


@app.post("/auth/phone/verify")
async def auth_phone_verify(payload: PhoneVerifyPayload) -> dict[str, Any]:
    """Шаг 2: проверяем код и выдаём токен сессии."""
    from app.services import phone_auth

    res = await phone_auth.verify_code(payload.token, payload.code)
    if not res.get("ok"):
        raise HTTPException(status_code=403, detail=res.get("error", "ошибка"))
    return {"session": res["session"], "user_id": res["user_id"]}


@app.post("/auth/phone/status")
async def auth_phone_status(payload: dict) -> dict[str, Any]:
    """Приложение опрашивает: код уже отправлен ботом?

    Нужно, чтобы экран сам перешёл к вводу кода, когда пользователь
    поделился номером в Телеграме, — без кнопки «я уже нажал».
    """
    token = str(payload.get("token") or "")
    rows = await repo.fetch(
        "SELECT code IS NOT NULL AS sent FROM auth_requests WHERE token = %s",
        (token,))
    return {"sent": bool(rows and rows[0]["sent"])}


# ------------------------------------------------------------------ Fitbit

@app.get("/fitbit/connect", include_in_schema=False)
async def fitbit_connect(uid: int, request: Request):
    """Старт OAuth Google Health: открой эту ссылку через https-туннель."""
    if not fitbit.enabled():
        return HTMLResponse("Fitbit не настроен: нет FITBIT_CLIENT_ID/SECRET в .env",
                            status_code=503)
    # redirect строится от хоста запроса, но схема — всегда https:
    # за туннелем uvicorn видит http, и Google отвечал redirect_uri_mismatch
    redirect = "https://" + request.url.netloc + "/fitbit/callback"
    return RedirectResponse(fitbit.authorize_url(uid, redirect))


@app.get("/fitbit/callback", include_in_schema=False)
async def fitbit_callback(state: str, code: str | None = None, error: str | None = None):
    if error or not code:
        return HTMLResponse(f"<meta charset='utf-8'>Google вернул ошибку: {error or 'нет кода'}")
    user_id, redirect = fitbit.unpack_state(state)
    await fitbit.exchange_code(user_id, code, redirect)
    synced = await fitbit.sync_user(user_id, days=3)
    lines = "".join(f"<li>{d}: {h} ч</li>" for d, h in sorted(synced.items()))
    return HTMLResponse(
        "<meta charset='utf-8'><body style='font-family:-apple-system;padding:40px'>"
        "<h2>✅ Fitbit (Google Health) подключён</h2><p>Сон подтянут:</p>"
        f"<ul>{lines or '<li>данных за 3 дня не нашлось — проверю в логах формат ответа</li>'}</ul>"
        "<p>Дальше сервер сам синхронизирует каждый час.</p></body>")
