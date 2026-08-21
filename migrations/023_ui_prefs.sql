-- Настройки интерфейса на пользователя: состав виджетов, карточек,
-- графиков и целей. Нужны, чтобы приложение и веб показывали одно и
-- то же: поменял в одном — увидел в другом.
CREATE TABLE IF NOT EXISTS ui_prefs (
    user_id BIGINT PRIMARY KEY,
    prefs   JSONB  NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
