-- Каталог блюд наружу: единица порции («г» или «шт») и вес одной штуки.
-- default_grams всегда в граммах; при unit='шт' порция показывается как
-- default_grams / unit_grams штук.
ALTER TABLE my_foods
    ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'г',
    ADD COLUMN IF NOT EXISTS unit_grams NUMERIC(6, 1);
