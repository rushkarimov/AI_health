-- Кислород во сне и температура кожи — появятся с браслета через
-- несколько ночей, колонки готовим заранее.
ALTER TABLE health_daily
    ADD COLUMN IF NOT EXISTS spo2_pct REAL,
    ADD COLUMN IF NOT EXISTS skin_temp_c REAL;
