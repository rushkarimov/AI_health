-- Средний пульс за день — возвращаем колонку.
--
-- Её удаляла миграция 008: за 217 дней данные приходили один день, потому что
-- в «Быстрых командах» метрика не была настроена. Сейчас настраивается, и
-- пульс в приложении «Здоровье» есть — значит колонке снова есть что хранить.
--
-- Отличается от resting_hr: тот — пульс покоя (нижняя граница за сутки),
-- а heart_rate_avg — средний по всем замерам. Обе величины нужны «Психологу»:
-- покой показывает восстановление, средний — общую нагрузку дня.

ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS heart_rate_avg NUMERIC;

-- Вьюхи пересобираем: v_daily_full отдаёт health_daily.* через явный список
-- колонок, и без пересборки новая колонка в разделы не попадёт.
-- Порядок как в 008: цепочка v_health_filled → v_daily_nutrition → v_daily_full.

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
    user_id        AS "пользователь"
FROM v_daily_full
ORDER BY day DESC;
