"""Точка входа бота.

Работает на polling — не нужен внешний IP и HTTPS-сертификат, что важно
для запуска на домашней машине или дешёвом VPS без домена.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.config import get_settings
from app.db.pool import close_pool, run_migrations
from app.handlers import commands, labs, meals, psy
from app import tracing
from app.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


class AllowlistMiddleware(BaseMiddleware):
    """Пускаем своих: список из .env плюс зарегистрированные через мини-апп.

    Токен Telegram легко утекает, а бот тратит деньги на API — без этого
    фильтра любой, кто узнает имя бота, будет расходовать твою квоту.

    ALLOWED_USER_IDS оставлен как «аварийный вход»: он работает, даже если база
    недоступна, и им же заводится первый пользователь. Остальные приходят через
    таблицу users — её наполняет форма регистрации, проверив код-приглашение.
    """

    def __init__(self, allowed: list[int]) -> None:
        self.allowed = set(allowed)
        # Кеш, чтобы не ходить в базу на каждое сообщение: регистрация —
        # событие редкое, а middleware вызывается на каждый апдейт.
        self._known: set[int] = set()

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        if user.id in self.allowed or user.id in self._known:
            return await self._handle_measured(handler, event, data)

        from app.db import repo

        try:
            registered = await repo.is_registered(user.id)
        except Exception:
            # база недоступна — пропускаем только тех, кто в .env: пускать всех
            # при сбое нельзя, а отказывать своим обидно
            log.exception("Не удалось проверить регистрацию %s", user.id)
            registered = False

        if registered:
            self._known.add(user.id)
            return await self._handle_measured(handler, event, data)

        log.warning("Незарегистрированный пользователь %s (%s)", user.id, user.username)
        await _offer_registration(event)
        return None


    async def _handle_measured(self, handler, event, data):
        """Обработка с замером времени и типа обращения.

        Считаем здесь, а не в каждом хендлере: middleware — единственная точка,
        через которую проходят все апдейты, и по её метрикам видно и нагрузку,
        и деградацию времени ответа.
        """
        from app import metrics

        kind = _update_kind(event)
        metrics.inc("updates", kind=kind)
        try:
            with metrics.timer("handler_latency", kind=kind):
                return await handler(event, data)
        except Exception:
            metrics.inc("errors", where="handler")
            raise


def _update_kind(event) -> str:
    """Тип обращения для метрик: photo | voice | text | callback | other."""
    if getattr(event, "data", None) is not None:
        return "callback"
    if getattr(event, "photo", None):
        return "photo"
    if getattr(event, "voice", None) or getattr(event, "audio", None):
        return "voice"
    if getattr(event, "text", None):
        return "text"
    return "other"


async def _offer_registration(event) -> None:
    """Незнакомцу — кнопка с формой вместо молчания.

    Раньше бот просто игнорировал чужие сообщения, и человек не понимал,
    сломался бот или его не пустили.
    """
    from aiogram.types import (
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Message,
        WebAppInfo,
    )

    s = get_settings()
    if not s.webapp_url:
        return

    message = event if isinstance(event, Message) else getattr(event, "message", None)
    if message is None:
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Зарегистрироваться",
                             web_app=WebAppInfo(url=s.webapp_url)),
    ]])
    try:
        await message.answer(
            "Привет! Этот бот работает по приглашению.\n\n"
            "Если у тебя есть код — заполни короткую форму, и всё откроется.",
            reply_markup=kb,
        )
    except Exception:
        log.exception("Не удалось предложить регистрацию %s", message.chat.id)


async def main() -> None:
    s = get_settings()
    if not s.bot_token:
        raise SystemExit("BOT_TOKEN не задан — заполни .env")

    # Метрики инициализируем при старте, а не при первом событии: в
    # multiproc-режиме prometheus_client создаёт файлы счётчиков заранее, и без
    # этого /metrics не видел бы метрик бота, пока кто-нибудь не напишет боту.
    from app import metrics

    metrics.setup_multiproc()
    metrics.warmup()

    await run_migrations()

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher()
    dp.message.middleware(AllowlistMiddleware(s.allowed_user_ids))
    dp.callback_query.middleware(AllowlistMiddleware(s.allowed_user_ids))

    # порядок важен: команды раньше «любого текста»
    dp.include_router(commands.router)
    # labs и psy до meals: иначе фото бланка анализов уйдёт в распознавание еды,
    # а роутер еды ловит любое фото
    dp.include_router(labs.router)
    dp.include_router(psy.router)
    dp.include_router(meals.router)

    scheduler = setup_scheduler(bot)

    log.info("Бот запущен. STT=%s VISION=%s LLM=%s",
             s.stt_provider, s.vision_provider, s.llm_provider)
    try:
        # Порядок как в нижнем меню (main_menu_kb), поддержка последней —
        # так человек находит одно и то же в двух местах на тех же позициях.
        # /repeat сюда не выводим: он редкий и есть в сетке /start.
        await bot.set_my_commands([
            BotCommand(command="start", description="🚀 Меню и все разделы"),
            BotCommand(command="today", description="📊 Сегодня"),
            BotCommand(command="health", description="📈 Здоровье"),
            BotCommand(command="labs", description="🏥 Анализы"),
            BotCommand(command="psy", description="🫂 Психолог"),
            BotCommand(command="goal", description="🎯 Цель"),
            BotCommand(command="advice", description="🎓 Рекомендации"),
            BotCommand(command="sleep", description="😴 Сон: фазы и оценка"),
            BotCommand(command="foods", description="📖 Мои продукты"),
            BotCommand(command="sync", description="🔄 Источники данных"),
            BotCommand(command="support", description="🛠 Поддержка"),
            # /charts из меню убран: графики и так приходят внутри «Здоровья»
            # и «Рекомендаций», а в списке дублировали «📈 Здоровье» и эмодзи,
            # и смысл. Сама команда работает — просто не мелькает в гамбургере.
            #
            # Две правки данных — в конце списка: ими пользуются реже, чем
            # просмотром, и обе открывают мини-апп.
            BotCommand(command="profile", description="👤 Изменить анкету"),
            BotCommand(command="last", description="🍽 Изменить съеденные блюда"),
        ])
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
        # досылаем накопленные трейсы: без flush последние теряются в буфере
        tracing.flush()
        await close_pool()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
