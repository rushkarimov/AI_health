"""Пул соединений и накат миграций."""
from __future__ import annotations

import logging
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from app.config import get_settings

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(get_settings().dsn, min_size=1, max_size=8, open=False)
        await _pool.open(wait=True, timeout=30)
        log.info("Пул Postgres открыт")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def run_migrations() -> None:
    """Простейший накат: прогоняем все .sql по порядку.

    Скрипты идемпотентны (IF NOT EXISTS / DROP + CREATE), поэтому таблица
    версий на этом масштабе — лишняя сложность.

    Каждый файл — в своей транзакции. Раньше все шли в одной, и поздняя
    миграция падала с "cannot drop columns from view": вьюха, созданная
    в 001, внутри той же транзакции ещё не была видна как существующая,
    и DROP IF EXISTS её не удалял.
    """
    pool = await get_pool()
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    # uvicorn поднимает несколько воркеров, и каждый зовёт накат при старте.
    # Без общей блокировки два процесса одновременно делают DROP + CREATE VIEW
    # и ловят взаимную блокировку. Advisory-lock держится до конца сессии:
    # второй воркер ждёт, а потом проходит по идемпотентным скриптам вхолостую.
    async with pool.connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(4915623)")
        try:
            for f in files:
                async with pool.connection() as conn:
                    await conn.execute(f.read_text(encoding="utf-8"))
                log.info("Миграция применена: %s", f.name)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock(4915623)")
