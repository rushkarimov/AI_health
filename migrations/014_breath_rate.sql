-- Частота дыхания во сне (вдохов/мин) — приходит из Google Health API.
ALTER TABLE health_daily
    ADD COLUMN IF NOT EXISTS breath_rate REAL;
