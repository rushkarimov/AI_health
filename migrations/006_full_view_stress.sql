-- Добавляет метрики стресса и фазы сна в v_daily_full.
--
-- Миграция 005 добавила колонки в health_daily, но вьюха перечисляет поля
-- списком, и новые в неё не попали — раздел «Психолог» падал с
-- UndefinedColumn: column "sleep_deep_hours" does not exist.
--
-- CREATE OR REPLACE VIEW не проходит: меняется набор колонок, Postgres такое
-- запрещает. Поэтому DROP и пересоздание.
--
-- Структуру исходной вьюхи (003) НЕ меняем: она строится на v_daily_nutrition,
-- где уже посчитаны eaten/burned/balance с приоритетом своих записей над
-- dietary_* из Health. Переписывать эту логику здесь незачем — только
-- дописываем к списку новые поля из health_daily.

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
    -- новое: метрики стресса и фазы сна для раздела «Психолог»
    h.hrv_ms,
    h.heart_rate_avg
FROM v_daily_nutrition n
LEFT JOIN health_daily h
       ON h.user_id = n.user_id AND h.day = n.day;


-- Русская вьюха поверх обновлённой — восстанавливаем после DROP
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
    steps           AS "шаги",
    distance_km     AS "км",
    sleep_hours     AS "сон",
    resting_hr      AS "пульс_покоя",
    hrv_ms          AS "вср",
    user_id         AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;
