-- Память бота: короткая (история диалога) и долгая (факты о пользователе).
--
-- Раньше каждое сообщение обрабатывалось с нуля: сказал имя — через минуту
-- бот его не помнил. Два уровня вместо одного, потому что задачи разные:
--
--   messages — контекст последних минут: «а сколько это в белках?» должно
--   понимать, о чём шла речь. Живёт недолго, чистится по времени.
--
--   user_facts — то, что стоит помнить всегда: имя, ограничения в еде,
--   режим тренировок. Компактно, поэтому не раздувает промпт.

CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    role       TEXT        NOT NULL CHECK (role IN ('user', 'assistant')),
    text       TEXT        NOT NULL,
    intent     TEXT,                       -- как классифицировал роутер
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- индекс под основной запрос: последние N сообщений пользователя
CREATE INDEX IF NOT EXISTS idx_messages_user_time
    ON messages (user_id, created_at DESC);


CREATE TABLE IF NOT EXISTS user_facts (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT      NOT NULL,
    fact       TEXT        NOT NULL,
    -- откуда узнали: пригодится, если факт окажется неверным
    source     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- один и тот же факт не дублируем
    UNIQUE (user_id, fact)
);
CREATE INDEX IF NOT EXISTS idx_user_facts_user ON user_facts (user_id);


-- Русская вьюха для чтения глазами, как остальные
DROP VIEW IF EXISTS "v_память";
CREATE VIEW "v_память" AS
SELECT
    created_at AS "когда",
    role       AS "кто",
    text       AS "текст",
    intent     AS "намерение",
    user_id    AS "пользователь"
FROM messages
ORDER BY created_at DESC;

DROP VIEW IF EXISTS "v_факты";
CREATE VIEW "v_факты" AS
SELECT
    fact       AS "факт",
    source     AS "откуда",
    created_at AS "узнали",
    user_id    AS "пользователь"
FROM user_facts
ORDER BY created_at DESC;
