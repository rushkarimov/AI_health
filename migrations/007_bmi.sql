-- Индекс массы тела из Apple Health.
--
-- Считать его самим было бы можно, но для этого нужен рост, а его в HealthKit
-- пишут не все и не всегда. Apple Health отдаёт готовый body_mass_index,
-- поэтому проще принимать значение как есть.

ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS bmi NUMERIC;

-- Добавляем в сводную вьюху: DROP и пересоздание, потому что CREATE OR REPLACE
-- не разрешает менять набор колонок.
DROP VIEW IF EXISTS "v_дни";
DROP VIEW IF EXISTS v_daily_full;

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
    h.dietary_kcal,
    h.hrv_ms,
    h.heart_rate_avg,
    h.bmi
FROM v_daily_nutrition n
LEFT JOIN health_daily h
       ON h.user_id = n.user_id AND h.day = n.day;

CREATE VIEW "v_дни" AS
SELECT
    day             AS "дата",
    kcal_eaten      AS "съел",
    kcal_burned     AS "потратил",
    balance         AS "баланс",
    protein         AS "белки",
    fat             AS "жиры",
    carbs           AS "углеводы",
    meals_count     AS "приёмов",
    weight_kg       AS "вес",
    body_fat_pct    AS "жир_проц",
    bmi             AS "имт",
    steps           AS "шаги",
    distance_km     AS "км",
    sleep_hours     AS "сон",
    resting_hr      AS "пульс_покоя",
    hrv_ms          AS "вср",
    user_id         AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;
