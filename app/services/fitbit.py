"""Сон с браслета Fitbit — напрямую из облака через Google Health API.

Зачем: браслет пишет в Apple Здоровье только при открытом приложении,
поэтому сон доезжал кусками (2 ч из 7). Сервер тянет «источник правды»
сам по расписанию.

История: сначала был Fitbit Web API (dev.fitbit.com), но с 2026-го новые
приложения регистрируют только через Google Health API — Google перевёл
Fitbit на свою инфраструктуру. OAuth теперь гугловский, эндпоинты
health.googleapis.com, скоупы googlehealth.*.

Настройка: проект в Google Cloud Console → включить «Google Health API» →
OAuth consent (External, Testing, свой e-mail в test users) → OAuth client
(Web application, redirect на https-туннель) → FITBIT_CLIENT_ID /
FITBIT_CLIENT_SECRET в .env сервера.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

import httpx

from psycopg.types.json import Jsonb

from app.db import repo

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://health.googleapis.com/v4"
# сон + витальные метрики (пульс покоя)
SCOPES = ("https://www.googleapis.com/auth/googlehealth.sleep.readonly "
          "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly "
          # калории и дистанция: в приложении Fitbit источником стоит Apple
          # Health, так что это те же данные, только доезжают сами
          "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly")

# фазы, которые считаются сном (AWAKE и короткие пробуждения — нет)
_ASLEEP_STAGES = {"LIGHT", "DEEP", "REM", "ASLEEP", "SLEEP"}


def client_id() -> str | None:
    return os.getenv("FITBIT_CLIENT_ID") or None


def _secret() -> str | None:
    return os.getenv("FITBIT_CLIENT_SECRET") or None


def enabled() -> bool:
    return bool(client_id() and _secret())


def _pack_state(user_id: int, redirect: str) -> str:
    raw = json.dumps({"u": user_id, "r": redirect}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def unpack_state(state: str) -> tuple[int, str]:
    d = json.loads(base64.urlsafe_b64decode(state.encode()))
    return int(d["u"]), d["r"]


def authorize_url(user_id: int, redirect: str) -> str:
    """redirect приходит с текущего хоста запроса: работает и через
    https-туннель (нужен Google), и напрямую по IP, если политика сменится."""
    from urllib.parse import urlencode
    return AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect,
        "scope": SCOPES,
        "state": _pack_state(user_id, redirect),
        "access_type": "offline",   # иначе Google не даст refresh_token
        "prompt": "consent",
    })


async def exchange_code(user_id: int, code: str, redirect: str) -> None:
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": client_id(),
            "client_secret": _secret(),
        })
    r.raise_for_status()
    await _save_tokens(user_id, r.json())


async def _save_tokens(user_id: int, tok: dict) -> None:
    expires = datetime.now(timezone.utc) + timedelta(seconds=tok.get("expires_in", 3600))
    refresh = tok.get("refresh_token")
    if refresh:
        await repo.execute(
            """INSERT INTO fitbit_tokens (user_id, access_token, refresh_token, expires_at)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET access_token = EXCLUDED.access_token,
                   refresh_token = EXCLUDED.refresh_token, expires_at = EXCLUDED.expires_at""",
            (user_id, tok["access_token"], refresh, expires))
    else:
        # refresh Google возвращает только при первом consent — сохраняем старый
        await repo.execute(
            "UPDATE fitbit_tokens SET access_token = %s, expires_at = %s WHERE user_id = %s",
            (tok["access_token"], expires, user_id))


async def connected(user_id: int) -> bool:
    rows = await repo.fetch(
        "SELECT 1 FROM fitbit_tokens WHERE user_id = %s", (user_id,))
    return bool(rows)


async def _access_token(user_id: int) -> str | None:
    rows = await repo.fetch(
        "SELECT access_token, refresh_token, expires_at FROM fitbit_tokens "
        "WHERE user_id = %s", (user_id,))
    if not rows:
        return None
    row = rows[0]
    if row["expires_at"] > datetime.now(timezone.utc) + timedelta(minutes=5):
        return row["access_token"]
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": row["refresh_token"],
            "client_id": client_id(),
            "client_secret": _secret(),
        })
    if r.status_code != 200:
        log.warning("Google Health refresh не прошёл (%s): %s", r.status_code, r.text[:200])
        return None
    tok = r.json()
    await _save_tokens(user_id, tok)
    return tok["access_token"]


async def _get(token: str, path: str, params: dict | None = None) -> dict | None:
    async with httpx.AsyncClient(timeout=25) as http:
        r = await http.get(API + path, params=params or {},
                           headers={"Authorization": f"Bearer {token}",
                                    "Accept": "application/json"})
    if r.status_code != 200:
        log.warning("Google Health GET %s → %s: %.300s", path, r.status_code, r.text)
        return None
    return r.json()


def _iso(v: object) -> datetime | None:
    """Парсит таймстемпы API (RFC3339, бывают с Z и с долями секунд)."""
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _walk(node: object, found: list[tuple[datetime, datetime, str]]) -> None:
    """Собирает интервалы фаз сна из ответа, не завязываясь на точную схему.

    Google Health отдаёт сессии сна со стадиями (LIGHT/DEEP/REM/AWAKE);
    имена полей могут отличаться от документации, поэтому идём по дереву
    и берём любые объекты вида {*start*: ts, *end*: ts, *stage|type*: str}.
    """
    if isinstance(node, list):
        for item in node:
            _walk(item, found)
        return
    if not isinstance(node, dict):
        return
    start = end = None
    stage = None
    for key, value in node.items():
        low = key.lower()
        if "start" in low and (ts := _iso(_deep_time(value))):
            start = ts
        elif "end" in low and (ts := _iso(_deep_time(value))):
            end = ts
        elif low in ("stage", "type", "sleepstage", "stagetype") and isinstance(value, str):
            stage = value.upper()
    if start and end and stage:
        found.append((start, end, stage))
    for value in node.values():
        _walk(value, found)


def _deep_time(value: object) -> object:
    """start/end бывают строкой, а бывают объектом {time: ..., offset: ...}."""
    if isinstance(value, dict):
        for k in ("time", "civilTime", "civil_time", "timestamp", "dateTime"):
            if k in value:
                return value[k]
    return value


async def _sleep_by_day(token: str) -> dict[str, dict]:
    """Сон по дням: часы, сегменты фаз (для гипнограммы) и минуты по фазам.

    Фильтры дат у API капризные — берём последнюю страницу списка и
    раскладываем по локальной дате окончания ночи. Записи одной ночи из
    разных источников не суммируем — берём самую полную.
    """
    data = await _get(token, "/users/me/dataTypes/sleep/dataPoints")
    if not data:
        return {}
    by_day: dict[str, dict] = {}
    for point in data.get("dataPoints", []):
        sleep = point.get("sleep") or {}
        interval = sleep.get("interval") or {}
        end = _iso(interval.get("endTime"))
        if not end:
            continue
        try:
            offset = int(str(interval.get("endUtcOffset", "0")).rstrip("s"))
        except ValueError:
            offset = 0
        day = (end + timedelta(seconds=offset)).date().isoformat()

        # сегменты фаз с локальным временем — для гипнограммы
        stages = []
        sums = {"deep": 0.0, "rem": 0.0, "light": 0.0, "awake": 0.0}
        for st in sleep.get("stages") or []:
            s0, e0 = _iso(st.get("startTime")), _iso(st.get("endTime"))
            stype = str(st.get("type", "")).upper()
            if not s0 or not e0 or e0 <= s0:
                continue
            mins = (e0 - s0).total_seconds() / 60
            key = {"DEEP": "deep", "REM": "rem", "LIGHT": "light",
                   "AWAKE": "awake", "SHORT_AWAKE": "awake"}.get(stype)
            if key:
                sums[key] += mins
            elif stype in ("ASLEEP", "SLEEP"):
                sums["light"] += mins       # classic-запись без фаз
                key = "light"
            else:
                continue
            local_s = s0 + timedelta(seconds=offset)
            local_e = e0 + timedelta(seconds=offset)
            stages.append({"s": local_s.strftime("%H:%M"),
                           "e": local_e.strftime("%H:%M"), "t": key})

        minutes = 0.0
        summary = sleep.get("summary") or {}
        if summary.get("minutesAsleep") is not None:
            minutes = float(summary["minutesAsleep"])
        else:
            minutes = sums["deep"] + sums["rem"] + sums["light"]
        if minutes <= 0:
            continue
        prev = by_day.get(day)
        if prev is None or minutes > prev["minutes"]:
            by_day[day] = {"minutes": minutes,
                           "hours": round(minutes / 60, 2),
                           "stages": stages, "sums": sums}
    if not by_day:
        log.info("Google Health sleep: сырой ответ %.1500s",
                 json.dumps(data, ensure_ascii=False))
    return by_day


def _sleep_score(minutes: float, sums: dict) -> int:
    """Оценка сна 0-100, откалибрована по Google (Fitbit).

    База — длительность против цели 8 ч; качество (доля глубокого+REM,
    цель 40%) и непрерывность лишь умножают её. Так короткий сон не
    может получить высокий балл: 5.5 ч даже с идеальными фазами ≈ 68,
    как в приложении Google. Без фаз — только длительность.
    """
    base = min(minutes / 480, 1.0)
    asleep = sums["deep"] + sums["rem"] + sums["light"]
    if asleep <= 0 or (sums["deep"] == 0 and sums["rem"] == 0):
        return round(base * 100)
    quality = min((sums["deep"] + sums["rem"]) / asleep / 0.4, 1.0)
    efficiency = asleep / (asleep + sums["awake"])
    continuity = max(0.0, min((efficiency - 0.7) / 0.25, 1.0))
    return round(100 * base * (0.75 + 0.15 * quality + 0.10 * continuity))


async def _rhr_by_day(token: str) -> dict[str, float]:
    """Пульс покоя по дням: dailyRestingHeartRate с датой-объектом."""
    data = await _get(token, "/users/me/dataTypes/daily-resting-heart-rate/dataPoints")
    if not data:
        return {}
    by_day: dict[str, float] = {}
    for point in data.get("dataPoints", []):
        rhr = point.get("dailyRestingHeartRate") or {}
        d, bpm = rhr.get("date") or {}, rhr.get("beatsPerMinute")
        if bpm and d.get("year"):
            key = f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
            by_day[key] = float(bpm)
    return by_day


def _date_key(d: dict) -> str | None:
    if not isinstance(d, dict) or not d.get("year"):
        return None
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


async def _daily_metric(token: str, dtype: str, obj_key: str,
                        value_key: str) -> dict[str, float]:
    """Дневные метрики вида {date: {y,m,d}, <value_key>: N} — HRV, дыхание."""
    data = await _get(token, f"/users/me/dataTypes/{dtype}/dataPoints")
    if not data:
        return {}
    by_day: dict[str, float] = {}
    for point in data.get("dataPoints", []):
        obj = point.get(obj_key) or {}
        key = _date_key(obj.get("date") or {})
        value = obj.get(value_key)
        if key and value is not None:
            try:
                by_day[key] = float(value)
            except (TypeError, ValueError):
                pass
    return by_day


async def _daily_any(token: str, dtype: str) -> dict[str, float]:
    """Метрики с неизвестной заранее схемой (SpO2, температура кожи):
    ищем в ответе пары «дата + первое число» по дереву."""
    data = await _get(token, f"/users/me/dataTypes/{dtype}/dataPoints")
    if not data or not data.get("dataPoints"):
        return {}
    by_day: dict[str, float] = {}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        key = _date_key(node.get("date") or {})
        if key and key not in by_day:
            for k, v in node.items():
                if k in ("date", "createTime", "updateTime"):
                    continue
                if isinstance(v, (int, float)) or (
                        isinstance(v, str) and v.replace(".", "", 1).isdigit()):
                    by_day[key] = float(v)
                    break
        for v in node.values():
            walk(v)

    walk(data)
    if not by_day:
        log.info("Google Health %s: сырой ответ %.1200s",
                 dtype, json.dumps(data, ensure_ascii=False))
    return by_day


async def _sample_metric(token: str, dtype: str, obj_key: str,
                         value_key: str, scale: float = 1.0) -> dict[str, float]:
    """Замеры с точным временем (вес, % жира): sampleTime.civilTime.date.
    Берём последний замер каждого дня (API отдаёт свежие первыми)."""
    data = await _get(token, f"/users/me/dataTypes/{dtype}/dataPoints")
    if not data:
        return {}
    by_day: dict[str, float] = {}
    for point in data.get("dataPoints", []):
        obj = point.get(obj_key) or {}
        civil = ((obj.get("sampleTime") or {}).get("civilTime") or {})
        key = _date_key(civil.get("date") or {})
        value = obj.get(value_key)
        if key and value is not None and key not in by_day:
            try:
                by_day[key] = float(value) * scale
            except (TypeError, ValueError):
                pass
    return by_day


async def _daily_rollup(token: str, dtype: str, days: int) -> dict[str, float]:
    """Калории/дистанция: list у этих типов запрещён, только суточные
    агрегаты POST dataPoints:dailyRollUp c CivilDateTime-границами."""
    end = date.today() + timedelta(days=1)     # диапазон закрыто-открытый
    start = end - timedelta(days=days + 1)
    body = {
        "range": {
            "start": {"date": {"year": start.year, "month": start.month, "day": start.day}},
            "end": {"date": {"year": end.year, "month": end.month, "day": end.day}},
        },
        "windowSizeDays": 1,
    }
    async with httpx.AsyncClient(timeout=25) as http:
        r = await http.post(
            f"{API}/users/me/dataTypes/{dtype}/dataPoints:dailyRollUp",
            json=body,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"})
    if r.status_code != 200:
        log.warning("Google Health dailyRollUp %s → %s: %.300s",
                    dtype, r.status_code, r.text)
        return {}
    by_day: dict[str, float] = {}
    for point in r.json().get("rollupDataPoints", []):
        key = _date_key((point.get("civilStartTime") or {}).get("date") or {})
        if not key:
            continue
        # значение — первое число внутри объекта метрики
        # ({"totalCalories": {"kcalSum": 1173.7}}, у дистанции — метры)
        for k, v in point.items():
            if k in ("civilStartTime", "civilEndTime") or not isinstance(v, dict):
                continue
            for vv in v.values():
                # шаги приходят строкой ("countSum": "4"), калории — числом
                if isinstance(vv, (int, float, str)):
                    try:
                        by_day[key] = float(vv)
                        break
                    except (TypeError, ValueError):
                        continue
    return by_day


async def sync_user(user_id: int, days: int = 2) -> dict[str, float]:
    """Подтягивает сон за последние N дней в health_daily.

    Шаги/калории не трогаем: они приходят с iPhone, двойной источник
    дал бы скачущие цифры.
    """
    token = await _access_token(user_id)
    if not token:
        return {}
    sleep = await _sleep_by_day(token)
    by_day = {d: v["hours"] for d, v in sleep.items()}
    rhr = await _rhr_by_day(token)
    hrv = await _daily_metric(token, "daily-heart-rate-variability",
                              "dailyHeartRateVariability",
                              "averageHeartRateVariabilityMilliseconds")
    breath = await _daily_metric(token, "daily-respiratory-rate",
                                 "dailyRespiratoryRate", "breathsPerMinute")
    # вес и % жира: весы Xiaomi → Apple Health → Google Health → мы
    weight = await _sample_metric(token, "weight", "weight",
                                  "weightGrams", scale=0.001)
    fat = await _sample_metric(token, "body-fat", "bodyFat", "percentage")
    spo2 = await _daily_any(token, "oxygen-saturation")
    temp = await _daily_any(token, "core-body-temperature")
    steps = await _daily_rollup(token, "steps", days)
    # первый числовой ключ heartRate — beatsPerMinuteAvg, он и нужен
    hr_avg = await _daily_rollup(token, "heart-rate", days)
    total_kcal = await _daily_rollup(token, "total-calories", days)
    active_kcal = await _daily_rollup(token, "active-energy-burned", days)
    dist = await _daily_rollup(token, "distance", days)
    result: dict[str, float] = {}
    for offset in range(days):
        d = date.today() - timedelta(days=offset)
        key = d.isoformat()
        values: dict = {}
        hours = by_day.get(key)
        if hours and 0 < hours <= 16:
            values["sleep_hours"] = hours
            result[key] = hours
            night = sleep[key]
            await repo.execute(
                """INSERT INTO sleep_nights (user_id, day, stages, deep_min,
                       rem_min, light_min, awake_min, score, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (user_id, day) DO UPDATE SET
                       stages = EXCLUDED.stages, deep_min = EXCLUDED.deep_min,
                       rem_min = EXCLUDED.rem_min, light_min = EXCLUDED.light_min,
                       awake_min = EXCLUDED.awake_min, score = EXCLUDED.score,
                       updated_at = now()""",
                (user_id, d, Jsonb(night["stages"]),
                 round(night["sums"]["deep"]), round(night["sums"]["rem"]),
                 round(night["sums"]["light"]), round(night["sums"]["awake"]),
                 _sleep_score(night["minutes"], night["sums"])))
        if rhr.get(key):
            values["resting_hr"] = rhr[key]
        if hrv.get(key):
            values["hrv_ms"] = hrv[key]
        if breath.get(key):
            values["breath_rate"] = breath[key]
        if spo2.get(key):
            # доли и проценты приводим к процентам
            values["spo2_pct"] = round(spo2[key] * 100 if spo2[key] <= 1
                                       else spo2[key], 1)
        if temp.get(key) and 20 < temp[key] < 45:
            values["skin_temp_c"] = round(temp[key], 2)
        if weight.get(key) and 30 < weight[key] < 250:
            values["weight_kg"] = round(weight[key], 1)
        if fat.get(key):
            pct = fat[key] * 100 if fat[key] <= 1 else fat[key]
            if 3 < pct < 70:
                values["body_fat_pct"] = round(pct, 1)
        if steps.get(key):
            values["steps"] = round(steps[key])
        if hr_avg.get(key) and 30 < hr_avg[key] < 220:
            values["heart_rate_avg"] = round(hr_avg[key], 1)
        if active_kcal.get(key):
            values["active_kcal"] = active_kcal[key]
            # в базе «всего» = active + resting, total из API раскладываем
            if total_kcal.get(key) and total_kcal[key] > active_kcal[key]:
                resting = round(total_kcal[key] - active_kcal[key])
                # браслет не носили полдня — его «всего» неполное, и базовые
                # выходят смешными (541 вместо ~1650). За прошедшие дни такие
                # значения не пишем: клетку заполнит Apple через «Команды»,
                # он считает базовый обмен за весь день независимо от ношения.
                if offset == 0 or resting >= 1200:
                    values["resting_kcal"] = resting
                elif (typical := await _typical_resting(user_id)):
                    # неполный день ношения: подставляем личную норму
                    values["resting_kcal"] = typical
        if dist.get(key):
            # distance приходит в миллиметрах (millimetersSum)
            values["distance_km"] = round(dist[key] / 1_000_000, 2)
        if values:
            await repo.upsert_health_day(user_id, d, values, source="google")
    if result:
        log.info("Fitbit/Google Health: user=%s сон %s", user_id, result)
    return result


async def _typical_resting(user_id: int) -> float | None:
    """Базовый обмен для дней, когда браслет носили не весь день.

    Часы сняты → Apple тоже не считает, клетку никто не заполнит. Берём
    личную медиану полноценных дней за месяц (история Apple Watch), а без
    истории — формулу Миффлина-Сан Жеора по профилю и последнему весу.
    """
    rows = await repo.fetch(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY resting_kcal) AS m
           FROM health_daily
           WHERE user_id = %s AND resting_kcal >= 1200
             AND day >= current_date - 30""", (user_id,))
    if rows and rows[0]["m"]:
        return round(float(rows[0]["m"]))

    prof = await repo.fetch(
        "SELECT sex, height_cm, date_part('year', age(birth_date)) AS age "
        "FROM users WHERE user_id = %s", (user_id,))
    w = await repo.fetch(
        "SELECT weight_kg FROM health_daily WHERE user_id = %s "
        "AND weight_kg IS NOT NULL ORDER BY day DESC LIMIT 1", (user_id,))
    if not prof or not w:
        return None
    height = float(prof[0]["height_cm"] or 175)
    age = float(prof[0]["age"] or 0)
    if not 10 <= age <= 100:
        age = 30      # в профиле бывает заглушка вместо даты рождения
    weight = float(w[0]["weight_kg"])
    bmr = 10 * weight + 6.25 * height - 5 * age
    bmr += 5 if (prof[0]["sex"] or "m") == "m" else -161
    return round(bmr)


async def sync_all() -> None:
    """Обходит всех подключённых пользователей (задача планировщика)."""
    if not enabled():
        return
    from app import metrics
    rows = await repo.fetch("SELECT user_id FROM fitbit_tokens")
    for row in rows:
        try:
            result = await sync_user(row["user_id"])
            metrics.inc("google_syncs", result="saved" if result else "empty")
        except Exception:
            metrics.inc("google_syncs", result="error")
            log.exception("Fitbit sync user=%s", row["user_id"])
