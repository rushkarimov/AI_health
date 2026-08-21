-- Настройки синхронизации на пользователя: какие источники включены и
-- в каком приоритете, плюс журнал последних поступлений для интерфейса.
CREATE TABLE IF NOT EXISTS sync_sources (
    user_id  BIGINT NOT NULL,
    source   TEXT   NOT NULL,          -- google | shortcuts | app | manual
    enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    rank     INT    NOT NULL,          -- больше = приоритетнее
    PRIMARY KEY (user_id, source)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id       BIGSERIAL PRIMARY KEY,
    user_id  BIGINT      NOT NULL,
    source   TEXT        NOT NULL,
    day      DATE,
    metrics  TEXT,                     -- перечень метрик через запятую
    count    INT         NOT NULL DEFAULT 0,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sync_log_user ON sync_log (user_id, at DESC);
