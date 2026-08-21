-- Прямая интеграция с облаком Fitbit: токены OAuth на пользователя.
-- Сон и пульс покоя тянем по API — полнее, чем цепочка браслет →
-- приложение → Apple Здоровье → «Команды».
CREATE TABLE IF NOT EXISTS fitbit_tokens (
    user_id       BIGINT PRIMARY KEY,
    access_token  TEXT        NOT NULL,
    refresh_token TEXT        NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    connected_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
