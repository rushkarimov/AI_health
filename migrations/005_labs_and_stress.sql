-- Анализы крови и метрики стресса.
--
-- lab_results: показатели из лабораторных анализов. Одна строка = один
-- показатель одной сдачи, а не вся сдача целиком: так проще строить динамику
-- по конкретному маркеру («гемоглобин за год») и не надо гадать со схемой
-- под каждую лабораторию.
--
-- Новые колонки в health_daily нужны разделу «Психолог»: без ВСР и вариаций
-- пульса ему нечего анализировать, кроме сна.

CREATE TABLE IF NOT EXISTS lab_results (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    taken_on    DATE        NOT NULL,           -- дата сдачи, а не записи
    name        TEXT        NOT NULL,           -- как в бланке: «Гемоглобин»
    canonical   TEXT,                           -- приведённое имя для группировки
    value       NUMERIC,
    unit        TEXT,
    ref_low     NUMERIC,                        -- границы нормы из бланка
    ref_high    NUMERIC,
    -- вне нормы считаем сами при вставке: границы у лабораторий разные,
    -- а сравнивать в каждом запросе неудобно
    flag        TEXT CHECK (flag IN ('low', 'normal', 'high')),
    lab         TEXT,                           -- название лаборатории
    source      TEXT,                           -- photo | voice | text
    raw_input   TEXT,                           -- что прислал пользователь
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, taken_on, name)
);
CREATE INDEX IF NOT EXISTS idx_lab_user_date
    ON lab_results (user_id, taken_on DESC);
CREATE INDEX IF NOT EXISTS idx_lab_canonical
    ON lab_results (user_id, canonical, taken_on DESC);


-- Метрики для раздела «Психолог». Apple Health отдаёт их при выгрузке,
-- но раньше мы их отбрасывали — колонок не было.
ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS hrv_ms NUMERIC;              -- ВСР, главный маркер стресса
-- средний пульс нужен вьюхам 005-008; 008 его больше не удаляет
ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS heart_rate_avg NUMERIC;

-- Остальные метрики (respiratory_rate, blood_oxygen_pct, heart_rate_max,
-- mindful_minutes, sleep_deep/rem_hours, stand_hours) отсюда УБРАНЫ: их
-- удаляет 008_drop_unused.sql, а цикл «добавить-удалить» на каждом старте
-- сжигал слоты колонок до лимита 1600 (см. комментарий в 002).


-- Русские вьюхи, как остальные в проекте
DROP VIEW IF EXISTS "v_анализы";
CREATE VIEW "v_анализы" AS
SELECT
    taken_on  AS "сдано",
    name      AS "показатель",
    value     AS "значение",
    unit      AS "единицы",
    CASE flag WHEN 'low' THEN 'ниже нормы'
              WHEN 'high' THEN 'выше нормы'
              ELSE 'норма' END AS "оценка",
    ref_low   AS "норма_от",
    ref_high  AS "норма_до",
    lab       AS "лаборатория",
    user_id   AS "пользователь"
FROM lab_results
ORDER BY taken_on DESC, name;

DROP VIEW IF EXISTS "v_стресс";
CREATE VIEW "v_стресс" AS
SELECT
    day               AS "дата",
    hrv_ms            AS "вср_мс",
    resting_hr        AS "пульс_покоя",
    heart_rate_avg    AS "пульс_средний",
    sleep_hours       AS "сон_всего",
    user_id           AS "пользователь"
FROM health_daily
ORDER BY day DESC;
