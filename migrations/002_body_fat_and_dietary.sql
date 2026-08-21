-- Поля, которые приходят с умных весов и из приложений питания через
-- Apple Health, но которым раньше некуда было писать.

-- Процент жира — приходит с весов вместе с массой.
ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS body_fat_pct NUMERIC(4, 1);

-- Съеденные калории по данным Apple Health (FatSecret и подобные пишут
-- туда сами). Держим отдельно от наших meals: это независимый источник, и
-- складывать их нельзя — будет двойной учёт.
ALTER TABLE health_daily ADD COLUMN IF NOT EXISTS dietary_kcal    NUMERIC(8, 1);

-- dietary_protein/fat/carbs и lean_body_mass_kg отсюда УБРАНЫ: их удаляет
-- 008_drop_unused.sql, а миграции гоняются целиком при каждом старте.
-- Пара «добавил в 002 → удалил в 008» на каждый рестарт сжигала слоты
-- колонок (Postgres считает удалённые в лимит 1600) — за сотню деплоев
-- таблица упёрлась в предел, и бот падал на старте.

-- Вьюха v_daily_full переехала в 003_totals_and_ru_views.sql: её состав
-- колонок изменился, а CREATE OR REPLACE здесь падал бы при каждом старте
-- с "cannot drop columns from view".
