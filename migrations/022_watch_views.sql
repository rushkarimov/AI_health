-- Метрики Apple Watch во вьюхах: минуты тренировок и VO2max.
-- Вьюхи пересоздаются целиком (как в 008/010/016).

-- Новые метрики Google Health во вьюхах: дыхание, SpO2, температура кожи.
-- Вьюхи пересоздаются целиком (как в 008/010).

DROP VIEW IF EXISTS "v_дни" CASCADE;
DROP VIEW IF EXISTS v_daily_full CASCADE;

CREATE VIEW v_daily_full AS
SELECT
    n.user_id,
    n.day,
    n.kcal            AS kcal_logged,
    n.protein, n.fat, n.carbs, n.meals_count,
    n.eaten_kcal      AS kcal_eaten,
    n.burned_kcal     AS kcal_burned,
    n.balance_kcal    AS balance,
    n.weight_kg,
    n.body_fat_pct,
    n.weight_measured,
    h.steps,
    h.active_kcal,
    h.resting_kcal,
    h.distance_km,
    h.sleep_hours,
    h.resting_hr,
    h.heart_rate_avg,
    h.hrv_ms,
    h.breath_rate,
    h.spo2_pct,
    h.skin_temp_c,
    h.exercise_min,
    h.vo2max,
    h.dietary_kcal,
    h.bmi
FROM v_daily_nutrition n
LEFT JOIN health_daily h
       ON h.user_id = n.user_id AND h.day = n.day;

CREATE VIEW "v_дни" AS
SELECT
    day            AS "дата",
    kcal_eaten     AS "съел",
    kcal_burned    AS "потратил",
    balance        AS "баланс",
    protein        AS "белки",
    fat            AS "жиры",
    carbs          AS "углеводы",
    meals_count    AS "приёмов",
    weight_kg      AS "вес",
    body_fat_pct   AS "жир_проц",
    bmi            AS "имт",
    steps          AS "шаги",
    distance_km    AS "км",
    sleep_hours    AS "сон",
    resting_hr     AS "пульс_покоя",
    heart_rate_avg AS "пульс_средний",
    hrv_ms         AS "вср",
    breath_rate    AS "дыхание",
    spo2_pct       AS "кислород",
    skin_temp_c    AS "темп_кожи",
    exercise_min   AS "минуты_тренировок",
    vo2max         AS "vo2max",
    user_id        AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;
