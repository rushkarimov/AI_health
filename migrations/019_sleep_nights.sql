-- Циклы сна: сегменты фаз за ночь (для гипнограммы) и оценка сна.
-- Отдельная таблица, а не колонки health_daily: там лимит колонок,
-- а stages — жирный jsonb.
CREATE TABLE IF NOT EXISTS sleep_nights (
    user_id    BIGINT NOT NULL,
    day        DATE   NOT NULL,
    stages     JSONB  NOT NULL DEFAULT '[]'::jsonb,  -- [{s,e,t}] ISO-время и фаза
    deep_min   REAL,
    rem_min    REAL,
    light_min  REAL,
    awake_min  REAL,
    score      INT,                                   -- 0-100
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, day)
);
