-- Метрики Apple Watch: минуты тренировок (кольцо «Активность») и VO2max.
ALTER TABLE health_daily
    ADD COLUMN IF NOT EXISTS exercise_min REAL,
    ADD COLUMN IF NOT EXISTS vo2max REAL;
