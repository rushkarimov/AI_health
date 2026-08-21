-- Приоритет источников на каждую метрику дня, как «источники» в Apple
-- Здоровье: google (API Fitbit/Google Health) > app (наше iOS-приложение,
-- задел на будущее) > shortcuts («Команды»). Источник ниже рангом не
-- затирает значение, записанное источником выше.
ALTER TABLE health_daily
    ADD COLUMN IF NOT EXISTS src JSONB NOT NULL DEFAULT '{}'::jsonb;
