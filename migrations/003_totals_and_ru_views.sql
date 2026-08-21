-- Общие калории, протяжка веса и русские вьюхи для чтения глазами.

-- ---------------------------------------------------------------- калории

-- Суммарный расход за день. GENERATED, а не обычная колонка: слагаемые уже
-- есть, и вычисляемое поле невозможно рассинхронить с ними при апсерте.
-- Если пришло только одно из двух — считаем то, что известно.
ALTER TABLE health_daily
    ADD COLUMN IF NOT EXISTS total_kcal NUMERIC(8, 1)
    GENERATED ALWAYS AS (
        CASE
            WHEN active_kcal IS NULL AND resting_kcal IS NULL THEN NULL
            ELSE COALESCE(active_kcal, 0) + COALESCE(resting_kcal, 0)
        END
    ) STORED;


-- ------------------------------------------------- протяжка веса и жира

-- Взвешиваешься редко, поэтому в графиках и сводках вес — это дыры.
-- Протягиваем НАЗАД: взвесился 7-го числа 93 кг → 6, 5, 4-е тоже 93.
-- То есть измерение описывает период, который к нему привёл.
--
-- Отдельная вьюха, а не UPDATE в таблице: реальные измерения должны
-- остаться отличимы от достроенных, иначе потом не понять, где факт.
DROP VIEW IF EXISTS v_health_filled CASCADE;
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
    h.dietary_kcal,
    h.weight_kg,
    h.body_fat_pct,
    -- ближайшее измерение вперёд по времени (включая текущий день)
    COALESCE(
        h.weight_kg,
        (SELECT w.weight_kg FROM health_daily w
          WHERE w.user_id = h.user_id AND w.day >= h.day AND w.weight_kg IS NOT NULL
          ORDER BY w.day LIMIT 1),
        -- если впереди измерений нет — тянем последнее известное назад
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

-- Заменяем v_daily_nutrition: раньше она знала только про еду. Теперь это
-- день целиком — съедено, потрачено (общие калории), баланс и вес с протяжкой.
DROP VIEW IF EXISTS v_daily_nutrition CASCADE;

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
    -- потрачено = общие калории (активность + покой)
    h.total_kcal                    AS burned_kcal,
    -- съедено: свои записи в приоритете, иначе данные из Apple Health
    COALESCE(n.kcal, h.dietary_kcal) AS eaten_kcal,
    COALESCE(n.kcal, h.dietary_kcal) - h.total_kcal AS balance_kcal,
    h.weight_filled                 AS weight_kg,
    h.body_fat_filled               AS body_fat_pct,
    h.weight_measured
FROM meals_by_day n
FULL JOIN v_health_filled h
       ON h.user_id = n.user_id AND h.day = n.day;


-- v_daily_full пересобираем полностью. Именно DROP, а не CREATE OR REPLACE:
-- состав колонок изменился, а REPLACE умеет только добавлять их в конец и
-- падает с "cannot drop columns from view". На это миграция уже спотыкалась.
DROP VIEW IF EXISTS v_daily_full;

-- Состав колонок должен покрывать repo.METRICS целиком: на нём работают
-- графики, тренды в сводках и averages(). Не хватило sleep_hours — и
-- averages() падал с UndefinedColumn.
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
    h.dietary_kcal
FROM v_daily_nutrition n
LEFT JOIN health_daily h
       ON h.user_id = n.user_id AND h.day = n.day;


-- ------------------------------------------------- русские вьюхи для чтения
-- Только для глаз: ноутбук, db.sh, ручные запросы. Код бота работает с
-- английскими именами — переименование колонок в таблицах потянуло бы
-- переписывание всего слоя данных и SQL в кавычках повсюду.

DROP VIEW IF EXISTS "v_дни";
CREATE VIEW "v_дни" AS
SELECT
    day                AS "дата",
    ROUND(eaten_kcal)  AS "съедено_ккал",
    ROUND(burned_kcal) AS "потрачено_ккал",
    ROUND(balance_kcal) AS "баланс_ккал",
    protein            AS "белки_г",
    fat                AS "жиры_г",
    carbs              AS "углеводы_г",
    meals_count        AS "приёмов_пищи",
    weight_kg          AS "вес_кг",
    body_fat_pct       AS "жир_процент",
    weight_measured    AS "вес_измерен",
    user_id            AS "пользователь"
FROM v_daily_nutrition
ORDER BY day DESC;


DROP VIEW IF EXISTS "v_здоровье";
CREATE VIEW "v_здоровье" AS
SELECT
    day               AS "дата",
    steps             AS "шаги",
    ROUND(active_kcal)  AS "активность_ккал",
    ROUND(resting_kcal) AS "покой_ккал",
    ROUND(total_kcal)   AS "всего_ккал",
    distance_km       AS "дистанция_км",
    sleep_hours       AS "сон_часов",
    resting_hr        AS "пульс_покоя",
    weight_filled     AS "вес_кг",
    body_fat_filled   AS "жир_процент",
    weight_measured   AS "вес_измерен",
    user_id           AS "пользователь"
FROM v_health_filled
ORDER BY day DESC;


-- Продукты: только то, что нужно для расчёта, без служебных полей.
-- Порядок колонок = порядок важности, чтобы при взгляде на таблицу сразу
-- видеть название, порцию и калорийность.
DROP VIEW IF EXISTS "v_продукты";
CREATE VIEW "v_продукты" AS
SELECT
    canonical_name AS "название",
    default_grams  AS "порция_г",
    kcal_100       AS "ккал_на_100г",
    protein_100    AS "белки_на_100г",
    fat_100        AS "жиры_на_100г",
    carbs_100      AS "углеводы_на_100г",
    source         AS "источник",
    alias          AS "как_называю",
    hits           AS "попаданий",
    user_id        AS "пользователь"
FROM my_foods
ORDER BY hits DESC, canonical_name;


DROP VIEW IF EXISTS "v_цели";
CREATE VIEW "v_цели" AS
SELECT
    goal_text   AS "цель",
    is_active   AS "активна",
    active_from AS "дата_постановки",
    user_id     AS "пользователь"
FROM goals
ORDER BY active_from DESC;


DROP VIEW IF EXISTS "v_приёмы_пищи";
CREATE VIEW "v_приёмы_пищи" AS
SELECT
    m.eaten_at                                AS "время",
    COALESCE(i.resolved_name, i.name)         AS "продукт",
    i.grams                                   AS "граммы",
    i.kcal                                    AS "ккал",
    i.protein                                 AS "белки_г",
    i.fat                                     AS "жиры_г",
    i.carbs                                   AS "углеводы_г",
    i.food_source                             AS "источник_нутриентов",
    i.confidence                              AS "уверенность",
    m.source                                  AS "канал_ввода",
    m.raw_input                               AS "исходная_фраза",
    m.user_id                                 AS "пользователь"
FROM meal_items i
JOIN meals m ON m.id = i.meal_id
ORDER BY m.eaten_at DESC;
