-- Профиль пользователя и самостоятельная регистрация по коду-приглашению.
--
-- Зачем: доступ к боту раньше держался на ALLOWED_USER_IDS в .env — чтобы
-- добавить человека, нужно было руками править файл и перезапускать контейнер.
-- Теперь ID попадает в базу сам, когда человек ввёл код-приглашение в мини-аппе.
--
-- В таблице только то, чего НЕ приносит Apple Health: пол, дата рождения,
-- рост. Вес, шаги, сон и пульс сюда не дублируем — они в health_daily, и две
-- копии одной величины неизбежно разошлись бы.

CREATE TABLE IF NOT EXISTS users (
    user_id      BIGINT      PRIMARY KEY,      -- telegram id, он же ключ везде
    name         TEXT        NOT NULL,
    sex          TEXT        CHECK (sex IN ('m', 'f')),
    birth_date   DATE,
    height_cm    NUMERIC(4, 1),
    -- Пол и рост нужны для ИМТ и нормы калорий (миграция 007 добавила ИМТ,
    -- но считать его было нечем: рост нигде не хранился).
    tz           TEXT        NOT NULL DEFAULT 'Europe/Moscow',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Возраст считаем на лету, а не храним: хранимый разошёлся бы с реальностью
-- в первый же день рождения.
DROP VIEW IF EXISTS "v_профиль";
CREATE VIEW "v_профиль" AS
SELECT
    name                                          AS "имя",
    CASE sex WHEN 'm' THEN 'муж' WHEN 'f' THEN 'жен' END AS "пол",
    birth_date                                    AS "дата_рождения",
    date_part('year', age(birth_date))::int       AS "возраст",
    height_cm                                     AS "рост_см",
    registered_at                                 AS "зарегистрирован",
    user_id                                       AS "пользователь"
FROM users;
