-- Схема БД трекера. Минимум таблиц, которого хватит на первый месяц.

-- Приём пищи: одна запись = один "завтрак/обед/перекус"
CREATE TABLE IF NOT EXISTS meals (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL,
    eaten_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT        NOT NULL CHECK (source IN ('photo', 'voice', 'text', 'combined')),
    raw_input    TEXT,                       -- расшифровка голоса / подпись к фото
    photo_file_id TEXT,                      -- telegram file_id, чтобы не хранить байты
    comment      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meals_user_time ON meals (user_id, eaten_at DESC);

-- Позиции внутри приёма пищи
CREATE TABLE IF NOT EXISTS meal_items (
    id          BIGSERIAL PRIMARY KEY,
    meal_id     BIGINT      NOT NULL REFERENCES meals (id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,        -- как назвал пользователь
    resolved_name TEXT,                      -- как нашлось в справочнике
    grams       NUMERIC(8, 1) NOT NULL,
    kcal        NUMERIC(8, 1) NOT NULL,
    protein     NUMERIC(6, 1) NOT NULL DEFAULT 0,
    fat         NUMERIC(6, 1) NOT NULL DEFAULT 0,
    carbs       NUMERIC(6, 1) NOT NULL DEFAULT 0,
    confidence  NUMERIC(3, 2),               -- уверенность распознавания 0..1
    food_source TEXT                         -- cache | fatsecret | llm | manual
);
CREATE INDEX IF NOT EXISTS idx_meal_items_meal ON meal_items (meal_id);

-- Кэш продуктов: главный ускоритель. Через 2 недели тут 90% твоей еды.
CREATE TABLE IF NOT EXISTS my_foods (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    alias       TEXT        NOT NULL,        -- нормализованное название, как ты его говоришь
    canonical_name TEXT     NOT NULL,        -- красивое имя для отчётов
    kcal_100    NUMERIC(6, 1) NOT NULL,      -- нутриенты НА 100 Г
    protein_100 NUMERIC(5, 1) NOT NULL DEFAULT 0,
    fat_100     NUMERIC(5, 1) NOT NULL DEFAULT 0,
    carbs_100   NUMERIC(5, 1) NOT NULL DEFAULT 0,
    default_grams NUMERIC(6, 1),             -- твоя обычная порция
    source      TEXT,                        -- fatsecret | llm | manual
    hits        INT         NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_my_foods_alias ON my_foods (user_id, alias);

-- Данные из Apple Health, один день = одна строка
CREATE TABLE IF NOT EXISTS health_daily (
    user_id        BIGINT NOT NULL,
    day            DATE   NOT NULL,
    steps          INT,
    active_kcal    NUMERIC(8, 1),
    resting_kcal   NUMERIC(8, 1),
    distance_km    NUMERIC(6, 2),
    sleep_hours    NUMERIC(4, 2),
    resting_hr     INT,
    weight_kg      NUMERIC(5, 2),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day)
);

-- Цели пользователя, свободный текст + числовые ориентиры
CREATE TABLE IF NOT EXISTS goals (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    goal_text   TEXT   NOT NULL,             -- "минус 5 кг к сентябрю"
    kcal_target INT,
    protein_target INT,
    active_from DATE   NOT NULL DEFAULT CURRENT_DATE,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_goals_user ON goals (user_id, is_active);

-- Сводка за день (v_daily_nutrition) переехала в 003_totals_and_ru_views.sql:
-- там она собирает не только еду, но и расход калорий с протянутым весом.
--
-- Здесь её определения быть не должно: миграции прогоняются при каждом старте,
-- и CREATE OR REPLACE со старым набором колонок падал бы на новой версии
-- с "cannot drop columns from view".
