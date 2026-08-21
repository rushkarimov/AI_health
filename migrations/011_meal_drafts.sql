-- Черновики записей о еде — то, что редактируется в мини-аппе.
--
-- Зачем таблица, а не память процесса: мини-апп открывается в вебвью и стучится
-- в API, а бот и API — РАЗНЫЕ контейнеры. Словарь в памяти бота из API не
-- виден, поэтому распознанный список кладём в базу и передаём в ссылку только
-- короткий id.
--
-- Черновик живёт до нажатия «Записать» в форме, после чего превращается в meal
-- и удаляется. Брошенные подчищаем по created_at: человек открыл редактор и
-- закрыл, не сохранив.

CREATE TABLE IF NOT EXISTS meal_drafts (
    id            TEXT        PRIMARY KEY,     -- короткий случайный id для URL
    user_id       BIGINT      NOT NULL,
    -- items целиком в JSON: набор полей у позиции меняется вместе с resolver,
    -- и отдельные колонки пришлось бы править каждый раз
    items         JSONB       NOT NULL,
    source        TEXT        NOT NULL,        -- photo | voice | text | combined
    raw_input     TEXT,
    photo_file_id TEXT,
    -- Правка существующей записи: id того meal, который заменяем. NULL — новая.
    meal_id       BIGINT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meal_drafts_user ON meal_drafts (user_id, created_at DESC);
