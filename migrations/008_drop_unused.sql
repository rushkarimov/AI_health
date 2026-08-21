-- Убираем метрики, которые больше не поступают.
--
-- Health Auto Export отключён, данные идут из «Быстрых команд» на iPhone —
-- там настроено 11 метрик. Остальные колонки навсегда останутся пустыми:
-- держать их значит показывать в разделах прочерки и обещать данные, которых
-- не будет.
--
-- Что удаляем и почему:
--   dietary_protein/fat/carbs — БЖУ из Health нигде в коде не использовались:
--     макросы бот считает из своих записей еды, а не из выгрузки
--   sleep_deep_hours, sleep_rem_hours — фазы сна Health не отдавал ни разу
--   respiratory_rate, blood_oxygen_pct — то же, ноль записей за 217 дней
--   heart_rate_avg, heart_rate_max — приходили 1 день из 217
--   lean_body_mass_kg — мышечная масса, 18 дней и почти не задействована
--   mindful_minutes, stand_hours — ноль и один день соответственно
--
-- Оставляем всё, что реально приходит: steps, distance_km, active_kcal,
-- resting_kcal, sleep_hours, resting_hr, hrv_ms, weight_kg, body_fat_pct,
-- dietary_kcal, bmi.
--
-- Порядок важен: от health_daily зависит цепочка вьюх
-- v_health_filled → v_daily_nutrition → v_daily_full → русские вьюхи.
-- Postgres не даёт удалить колонку, пока на неё смотрит хотя бы одна вьюха,
-- поэтому сносим всю цепочку и собираем заново.

DROP VIEW IF EXISTS "v_дни" CASCADE;
DROP VIEW IF EXISTS "v_стресс" CASCADE;
DROP VIEW IF EXISTS "v_здоровье" CASCADE;
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
    COALESCE(n.kcal, h.dietary_kcal) AS eaten_kcal,
    COALESCE(n.kcal, h.dietary_kcal) - h.total_kcal AS balance_kcal,
    h.weight_filled                 AS weight_kg,
    h.body_fat_filled               AS body_fat_pct,
    h.weight_measured
FROM meals_by_day n
FULL JOIN v_health_filled h
       ON h.user_id = n.user_id AND h.day = n.day;


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
    h.hrv_ms,
    h.dietary_kcal,
    h.bmi
FROM v_daily_nutrition n
LEFT JOIN health_daily h
       ON h.user_id = n.user_id AND h.day = n.day;


-- ------------------------------------------ русские вьюхи для чтения глазами

CREATE VIEW "v_дни" AS
SELECT
    day          AS "дата",
    kcal_eaten   AS "съел",
    kcal_burned  AS "потратил",
    balance      AS "баланс",
    protein      AS "белки",
    fat          AS "жиры",
    carbs        AS "углеводы",
    meals_count  AS "приёмов",
    weight_kg    AS "вес",
    body_fat_pct AS "жир_проц",
    bmi          AS "имт",
    steps        AS "шаги",
    distance_km  AS "км",
    sleep_hours  AS "сон",
    resting_hr   AS "пульс_покоя",
    hrv_ms       AS "вср",
    user_id      AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;


CREATE VIEW "v_здоровье" AS
SELECT
    day          AS "дата",
    steps        AS "шаги",
    distance_km  AS "км",
    active_kcal  AS "активность",
    resting_kcal AS "покой",
    total_kcal   AS "потрачено_всего",
    sleep_hours  AS "сон",
    resting_hr   AS "пульс_покоя",
    hrv_ms       AS "вср",
    weight_kg    AS "вес",
    body_fat_pct AS "жир_проц",
    bmi          AS "имт",
    dietary_kcal AS "съел_по_health",
    user_id      AS "пользователь"
FROM health_daily
ORDER BY day DESC;


CREATE VIEW "v_стресс" AS
SELECT
    day         AS "дата",
    hrv_ms      AS "вср_мс",
    resting_hr  AS "пульс_покоя",
    sleep_hours AS "сон",
    steps       AS "шаги",
    user_id     AS "пользователь"
FROM health_daily
ORDER BY day DESC;
