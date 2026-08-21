-- «Съел» считается только по нашим записям еды: fallback на dietary_kcal
-- из Apple Health убран. После удаления всех приёмов день показывал 105 —
-- фантомную «пищевую энергию», приехавшую в Apple Health со стороны.
-- Колонка dietary_kcal остаётся (видна агенту), но в итог не подмешивается.

DROP VIEW IF EXISTS "v_дни" CASCADE;
DROP VIEW IF EXISTS v_daily_full CASCADE;
DROP VIEW IF EXISTS v_daily_nutrition CASCADE;
DROP VIEW IF EXISTS v_health_filled CASCADE;

ALTER TABLE health_daily
    DROP COLUMN IF EXISTS dietary_protein,
    DROP COLUMN IF EXISTS dietary_fat,
    DROP COLUMN IF EXISTS dietary_carbs,
    DROP COLUMN IF EXISTS sleep_deep_hours,
    DROP COLUMN IF EXISTS sleep_rem_hours,
    DROP COLUMN IF EXISTS respiratory_rate,
    -- heart_rate_avg здесь НЕ трогаем: его возвращает 010_heart_rate.sql,
    -- и пара drop-add на каждом старте сжигала слот колонки
    DROP COLUMN IF EXISTS blood_oxygen_pct,
    DROP COLUMN IF EXISTS heart_rate_max,
    DROP COLUMN IF EXISTS lean_body_mass_kg,
    DROP COLUMN IF EXISTS mindful_minutes,
    DROP COLUMN IF EXISTS stand_hours;


-- ------------------------------------------------ вес и жир с протяжкой
-- Логика та же, что в 003: взвешиваешься редко, поэтому тянем ближайшее
-- измерение вперёд, а если впереди нет — последнее известное назад.

CREATE VIEW v_health_filled AS
SELECT
    h.user_id,
    h.day,
    h.steps,
    h.active_kcal,
    h.resting_kcal,
    h.total_kcal,
    h.distance_km,
    h.sleep_hours,
    h.resting_hr,
    h.hrv_ms,
    h.dietary_kcal,
    h.bmi,
    h.weight_kg,
    h.body_fat_pct,
    COALESCE(
        h.weight_kg,
        (SELECT w.weight_kg FROM health_daily w
          WHERE w.user_id = h.user_id AND w.day >= h.day AND w.weight_kg IS NOT NULL
          ORDER BY w.day LIMIT 1),
        (SELECT w.weight_kg FROM health_daily w
          WHERE w.user_id = h.user_id AND w.day <= h.day AND w.weight_kg IS NOT NULL
          ORDER BY w.day DESC LIMIT 1)
    ) AS weight_filled,
    COALESCE(
        h.body_fat_pct,
        (SELECT w.body_fat_pct FROM health_daily w
          WHERE w.user_id = h.user_id AND w.day >= h.day AND w.body_fat_pct IS NOT NULL
          ORDER BY w.day LIMIT 1),
        (SELECT w.body_fat_pct FROM health_daily w
          WHERE w.user_id = h.user_id AND w.day <= h.day AND w.body_fat_pct IS NOT NULL
          ORDER BY w.day DESC LIMIT 1)
    ) AS body_fat_filled,
    (h.weight_kg IS NOT NULL) AS weight_measured
FROM health_daily h;


-- ------------------------------------------------------- сводка по дням

CREATE VIEW v_daily_nutrition AS
WITH meals_by_day AS (
    SELECT
        m.user_id,
        (m.eaten_at AT TIME ZONE 'Europe/Moscow')::date AS day,
        ROUND(SUM(i.kcal))    AS kcal,
        ROUND(SUM(i.protein)) AS protein,
        ROUND(SUM(i.fat))     AS fat,
        ROUND(SUM(i.carbs))   AS carbs,
        COUNT(DISTINCT m.id)  AS meals_count
    FROM meals m
    JOIN meal_items i ON i.meal_id = m.id
    GROUP BY m.user_id, (m.eaten_at AT TIME ZONE 'Europe/Moscow')::date
)
SELECT
    COALESCE(n.user_id, h.user_id)  AS user_id,
    COALESCE(n.day, h.day)          AS day,
    n.kcal,
    n.protein,
    n.fat,
    n.carbs,
    COALESCE(n.meals_count, 0)      AS meals_count,
    h.total_kcal                    AS burned_kcal,
    n.kcal AS eaten_kcal,
    n.kcal - h.total_kcal AS balance_kcal,
    h.weight_filled                 AS weight_kg,
    h.body_fat_filled               AS body_fat_pct,
    h.weight_measured
FROM meals_by_day n
FULL JOIN v_health_filled h
       ON h.user_id = n.user_id AND h.day = n.day;



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
    user_id        AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;
