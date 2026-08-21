"""Клавиатуры бота."""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Постоянное меню внизу экрана. Текст кнопок = то, что прилетает в message.text,
# поэтому эти же строки разбираются в handlers/commands.py.
BTN_TODAY = "📊 Сегодня"
BTN_REPEAT = "🔁 Повторить"
BTN_LAST = "🍽 Последние"
BTN_HEALTH = "📈 Здоровье"
BTN_GOAL = "🎯 Цель"
BTN_ADVICE = "🎓 Рекомендации"
BTN_CHARTS = "📈 Графики"  # осталось для сетки /start и команды /charts
BTN_LABS = "🏥 Анализы"
BTN_PSY = "🫂 Психолог"
BTN_HELP = "📋 Функции"  # осталось командой /help, из меню убрана
BTN_SUPPORT = "🛠 Поддержка"

MENU_BUTTONS = (
    BTN_TODAY, BTN_REPEAT, BTN_LAST, BTN_HEALTH, BTN_LABS, BTN_PSY,
    BTN_ADVICE, BTN_CHARTS, BTN_GOAL, BTN_HELP, BTN_SUPPORT,
)


def start_grid_kb() -> InlineKeyboardMarkup:
    """Большая сетка разделов на /start — два столбца, как в каталожных ботах.

    callback_data вида "nav:<что>" разбирается в handlers/commands.py.
    """
    kb = InlineKeyboardBuilder()
    # порядок как в нижнем меню, чтобы не искать одно и то же в двух местах
    kb.button(text="📊 Сегодня", callback_data="nav:today")
    kb.button(text="📈 Здоровье", callback_data="nav:health")
    kb.button(text="🏥 Анализы", callback_data="nav:labs")
    kb.button(text="🫂 Психолог", callback_data="nav:psy")
    kb.button(text="🎯 Цель", callback_data="nav:goal")
    kb.button(text="🎓 Рекомендации", callback_data="nav:advice")
    # Оставлены только шесть основных разделов. «За неделю», «Баланс»,
    # «Графики» есть внутри «Здоровья» и «Рекомендаций», а «Повторить»
    # и «Последние» доступны командами /repeat и /last.
    kb.adjust(2, 2, 2)
    # Поддержка убрана из сетки: диагностика нужна редко, а место в списке
    # разделов занимает. Осталась командой /support и в меню Телеграма.
    return kb.as_markup()


def charts_kb(days: int = 30) -> InlineKeyboardMarkup:
    """Выбор графика и периода. Период едет в callback_data, чтобы при смене
    метрики не сбрасывался выбранный интервал."""
    kb = InlineKeyboardBuilder()
    for key, label in (
        ("overview", "📈 Обзор"),
        ("balance", "⚖️ Баланс калорий"),
        ("weight", "🏋 Вес и жир"),
        ("eaten", "🍽 Съедено"),
        ("burned", "🔥 Потрачено"),
        ("active", "🏃 Активность"),
        ("resting", "😴 Энергия покоя"),
        ("sleep", "🛏 Сон"),
        ("steps", "👣 Шаги"),
        ("distance", "📍 Дистанция"),
        ("hr", "❤️ Пульс покоя"),
        ("fat", "📉 Процент жира"),
        ("hrv", "💚 ВСР"),
        ("bmi", "📐 ИМТ"),
    ):
        kb.button(text=label, callback_data=f"chart:{key}:{days}")
    kb.adjust(1, 2, 2, 2, 2, 2, 2)

    kb.row(
        *[
            InlineKeyboardButton(
                text=("· " if d == days else "") + title,
                callback_data=f"period:{d}",
            )
            for d, title in ((7, "7 дней"), (30, "месяц"), (90, "3 мес"), (365, "год"))
        ]
    )
    kb.row(InlineKeyboardButton(text="📅 Свои даты", callback_data="period:custom"))
    return kb.as_markup()


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Меню под полем ввода — только то, чем пользуешься ежедневно.

    Повтор приёма пищи и список записей сюда не выведены: они нужны редко и
    доступны из сетки /start и командами /repeat, /last. Шесть кнопок внизу
    вместо восьми — меньше визуального шума над клавиатурой.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_HEALTH)],
            [KeyboardButton(text=BTN_LABS), KeyboardButton(text=BTN_PSY)],
            [KeyboardButton(text=BTN_GOAL), KeyboardButton(text=BTN_ADVICE)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Фото, голос или текст о еде",
    )


def confirm_kb(edit_url: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки под распознанным приёмом пищи.

    «Исправить» открывает мини-апп, где видно название, вес и ккал/100 г каждого
    блюда и всё это правится на месте. Раньше кнопка просила прислать граммы
    через запятую по порядку — переименовать блюдо или добавить забытое было
    нельзя вообще.

    Без edit_url (WEBAPP_URL не задан) кнопка правки не показывается: пустая
    ссылка в web_app роняет запрос к Telegram.
    """
    from aiogram.types import WebAppInfo

    first = [InlineKeyboardButton(text="✅ Записать", callback_data="meal:save")]
    if edit_url:
        first.append(InlineKeyboardButton(text="✏️ Исправить",
                                          web_app=WebAppInfo(url=edit_url)))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            first,
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="meal:cancel")],
        ]
    )


def repeat_kb(meals: list[dict]) -> InlineKeyboardMarkup:
    """Кнопки «повторить приём пищи» — то, что реально спасает удержание.

    Ручной ввод надоедает через две недели; повтор в один тап убирает
    основную причину бросить трекинг.
    """
    kb = InlineKeyboardBuilder()
    for m in meals:
        label = (m.get("items") or "")[:40]
        kb.button(text=f"{label} · {m['kcal']:.0f} ккал", callback_data=f"repeat:{m['id']}")
    kb.adjust(1)
    return kb.as_markup()


def labs_kb() -> InlineKeyboardMarkup:
    """Действия в разделе анализов."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Добавить анализы", callback_data="labs:add"),
            ],
            [
                InlineKeyboardButton(text="🤖 Проанализировать анализы", callback_data="labs:explain"),
            ],
        ]
    )
