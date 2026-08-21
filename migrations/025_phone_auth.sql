-- Вход по номеру телефона: код присылает наш же бот в Телеграм.
--
-- Схема: в приложении вводят номер → приложение ведёт в бота по ссылке
-- со стартовым параметром → пользователь жмёт Start и «Поделиться номером»
-- → бот сверяет номер и присылает код в этот же чат → код вводят в приложении.
--
-- Почему так, а не Gateway API: сообщения от собственного бота бесплатны,
-- а номер подтверждает сам Телеграм — от руки его не подделать.

-- телефон у пользователя: в E.164 без плюса, как отдаёт Telegram
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone text;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users (phone)
    WHERE phone IS NOT NULL;

-- Заявки на вход. Живут минутами, поэтому чистим по created_at, а не храним.
CREATE TABLE IF NOT EXISTS auth_requests (
    token       text PRIMARY KEY,          -- уходит в ссылку t.me/...?start=auth_<token>
    phone       text NOT NULL,             -- номер, введённый в приложении
    code        text,                      -- шестизначный код; NULL, пока бот не выдал
    user_id     bigint,                    -- заполняется, когда номер подтверждён
    attempts    int  NOT NULL DEFAULT 0,   -- попытки ввода кода: защита от перебора
    created_at  timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz               -- когда код введён верно
);

CREATE INDEX IF NOT EXISTS idx_auth_requests_phone
    ON auth_requests (phone, created_at DESC);

-- Сессии приложения: после ввода кода выдаём долгий токен, чтобы не
-- гонять пользователя через бота при каждом запуске.
CREATE TABLE IF NOT EXISTS app_sessions (
    token       text PRIMARY KEY,
    user_id     bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user ON app_sessions (user_id);
