"""Таблицы картинкой, а не текстом.

ASCII-таблицы в моноширинном блоке Телеграма ломались на узких экранах: рамки
из ┌┬┐ рассыпались при переносе строки, поэтому ширину приходилось держать в
30 символов и резать названия до «гречка с кур…». Картинка от ширины экрана не
зависит — Телеграм масштабирует её сам, и в неё влезает полное название.

Палитра взята из презентации ML-команды (banki-meet-ml): синий #0D8BFF →
фиолетовый #9641FF в шапке, зелёный #00D73C для нормы, оранжевый #FF7828 для
отклонений. Так таблицы бота и слайды выглядят одним продуктом.

Рисуем вручную прямоугольниками, а не через ax.table: тот не умеет ни градиент
в шапке, ни скруглённые углы, ни разную выключку в колонках.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Rectangle  # noqa: E402

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ палитра

BLUE = "#0D8BFF"
VIOLET = "#9641FF"
GREEN = "#00D73C"
ORANGE = "#FF7828"
TEXT = "#0B1020"
MUTED = "#5A6472"
BG = "#FFFFFF"
ZEBRA = "#F7F9FC"      # подложка чётных строк
BORDER = "#EAEEF3"

# Геометрия в дюймах: считать в пикселях бессмысленно, размер фигуры задаётся
# в дюймах, а плотность — через DPI. При DPI=180 строка в 0.34" даёт ~61 px,
# что на телефоне читается без зума.
DPI = 180
ROW_H = 0.34
HEAD_H = 0.42
TITLE_H = 0.46          # полоса заголовка над шапкой
PAD_X = 0.16
CHAR_W = 0.088          # ширина символа в дюймах при font-size 11
# Размеры иконок в одном месте: раньше числа были рассыпаны по вызовам
# _put_emoji, и иконки в шапке выходили крупнее строчных.
ROW_ICON = 0.15         # иконка слева от строки; вровень с текстом 10.5pt
HEAD_ICON = 0.13        # иконка над названием колонки


def _fig_png(fig) -> bytes:
    buf = io.BytesIO()
    # bbox_inches="tight" не берём: он режет по содержимому и ломает
    # рассчитанные поля, из-за чего у таблицы пропадает воздух по краям
    fig.savefig(buf, format="png", dpi=DPI, facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _gradient(ax, x: float, y: float, w: float, h: float,
              c1: str = BLUE, c2: str = VIOLET, radius: float = 0.0) -> None:
    """Горизонтальный градиент в прямоугольнике.

    imshow с массивом 1×256: цвета берём из LinearSegmentedColormap, чтобы
    переход шёл в RGB, как в CSS linear-gradient, а не через HSV-крюк.

    radius скругляет верхние углы: imshow всегда рисует прямоугольник, поэтому
    форму задаём отсечением по FancyBboxPatch. Патч уводим за нижнюю границу на
    высоту скругления — иначе низ шапки тоже скруглится и между ней и шапкой
    колонок появится белый серп.
    """
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("brand", [c1, c2])
    im = ax.imshow(
        np.linspace(0, 1, 256).reshape(1, -1),
        extent=(x, x + w, y, y + h),
        aspect="auto",
        cmap=cmap,
        zorder=1,
    )
    if radius > 0:
        clip = FancyBboxPatch(
            (x + radius, y - radius + radius), w - 2 * radius, h - radius,
            boxstyle=f"round,pad={radius}",
            transform=ax.transData, linewidth=0, facecolor="none",
        )
        ax.add_patch(clip)
        im.set_clip_path(clip)


# Цветные эмодзи картинками, а не текстом.
#
# matplotlib не умеет рисовать цветные эмодзи-шрифты: NotoColorEmoji и Apple
# Color Emoji хранят глифы растром (CBDT/sbix), библиотека их не читает и
# выводит квадратики-заглушки. Проверено и на чистом образе с установленным
# шрифтом — не помогает.
#
# Поэтому держим готовые PNG (вырезаны из Apple Color Emoji, см. историю git) и
# вставляем их через AnnotationBbox. Шрифт в контейнере не нужен, вид одинаковый
# везде. Каждая иконка обрезана по прозрачным полям и вписана в квадрат 144×144,
# иначе узкие (🕐) выглядели крупнее широких (👣) при одном zoom.
EMOJI_DIR = Path(__file__).resolve().parent.parent / "assets" / "emoji"

# Имя → файл. Ключи короткие, чтобы вызывающий код читался: icons=["fire", ...]
EMOJI = {
    "fire": "fire.png",        # 🔥 потраченные калории
    "plate": "plate.png",      # 🍽 съеденное
    "scales": "scales.png",    # ⚖️ разница
    "weight": "weight.png",    # 🏋 вес
    "bed": "bed.png",          # 🛏 сон
    "steps": "steps.png",      # 👣 дистанция
    "sleep": "sleep.png",      # 😴 сон в «Психологе»
    "heart": "heart.png",      # 💓 пульс и ВСР
    "brain": "brain.png",      # 🧠 состояние
    "blood": "blood.png",      # 🩸 анализы
    "date": "date.png",        # 📅 дата
    "chart": "chart.png",      # 📊 значение показателя
    "target": "target.png",    # 🎯 норма, границы диапазона
    "clock": "clock.png",      # 🕐 время приёма пищи
}

# Кеш загруженных картинок: таблица за неделю просит одну и ту же иконку
# семь раз, читать файл каждый раз незачем.
_emoji_cache: dict[str, Any] = {}


def _emoji_img(name: str):
    """Массив пикселей иконки или None, если файла нет.

    None вместо исключения: пропавшая картинка не должна ломать всю таблицу —
    строка просто нарисуется без значка.
    """
    if name in _emoji_cache:
        return _emoji_cache[name]

    fname = EMOJI.get(name)
    if not fname:
        _emoji_cache[name] = None
        return None

    path = EMOJI_DIR / fname
    if not path.exists():
        log.warning("Иконка не найдена: %s", path)
        _emoji_cache[name] = None
        return None

    try:
        img = plt.imread(str(path))
    except Exception:
        log.exception("Не удалось прочитать иконку %s", path)
        img = None
    _emoji_cache[name] = img
    return img


def _put_emoji(ax, name: str, x: float, y: float, size: float = ROW_ICON) -> bool:
    """Вставляет иконку в точку (x, y) данных. True — если получилось.

    zoom считаем от DPI: OffsetImage масштабирует по пикселям, а вся остальная
    геометрия здесь в дюймах — без пересчёта иконка прыгала бы при смене DPI.
    """
    img = _emoji_img(name)
    if img is None:
        return False

    from matplotlib.offsetbox import AnnotationBbox, OffsetImage

    # zoom считаем от DPI ФИГУРЫ, а не от константы: savefig(dpi=DPI) не меняет
    # fig.dpi, а OffsetImage масштабирует относительно него. С константой
    # иконка выходила в 1.5 раза крупнее строки и перекрывала соседние.
    zoom = size * ax.figure.dpi / img.shape[0]
    box = AnnotationBbox(
        OffsetImage(img, zoom=zoom, resample=True),
        (x, y), frameon=False, pad=0, box_alignment=(0.5, 0.5),
        annotation_clip=False,
    )
    box.set_zorder(5)
    ax.add_artist(box)
    return True


def _rounded(ax, x, y, w, h, color, radius=0.06, **kw):
    """Скруглённый прямоугольник. Радиус в дюймах, как и остальная геометрия."""
    patch = FancyBboxPatch(
        (x + radius, y + radius), w - 2 * radius, h - 2 * radius,
        boxstyle=f"round,pad={radius}",
        linewidth=0, facecolor=color, **kw,
    )
    ax.add_patch(patch)
    return patch


def render_table(
    title: str,
    header: Sequence[str],
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str] | None = None,
    colors: Sequence[Sequence[str | None]] | None = None,
    footer: str = "",
    icons: Sequence[str | None] | None = None,
    head_icons: Sequence[str | None] | None = None,
) -> bytes:
    """Таблица в PNG.

    title   — полоса-заголовок сверху, рисуется градиентом
    header  — подписи колонок; все пустые — шапка не рисуется совсем
    rows    — строки, уже отформатированные в строки: форматирование чисел
              остаётся на вызывающей стороне, здесь только раскладка
    aligns  — "l" | "r" на колонку, по умолчанию первая влево, остальные вправо
    colors  — цвет текста ячейки или None; так «↑ферритин» краснеет, не таща
              за собой раскраску всей строки
    footer  — строка под таблицей: «всего 1842 ккал за 4 приёма»
    icons   — имя цветной иконки слева на каждую СТРОКУ (см. EMOJI)
    head_icons — то же, но над каждой КОЛОНКОЙ, для широких таблиц по дням
    """
    if not rows:
        raise ValueError("нечего рисовать: пустой список строк")

    ncol = len(header)
    if aligns is None:
        aligns = ["l"] + ["r"] * (ncol - 1)

    # Шапка из пустых строк не нужна: в сводке дня колонки без названий
    # («потратил / 497 ккал»), и пустая полоса только съедала высоту.
    show_head = any(str(h).strip() for h in header)
    # Заголовок в две строки («потратил \n ккал») — единицы уезжают под
    # название вместо того, чтобы повторяться в каждой ячейке.
    head_lines = max((str(h).count("\n") + 1 for h in header), default=1)
    head_h = (HEAD_H + (head_lines - 1) * 0.20) if show_head else 0.0
    # иконка над названием колонки требует своей полосы в шапке
    if show_head and head_icons:
        head_h += HEAD_ICON + 0.16
    # место под иконку слева
    dot_w = 0.30 if icons else 0.0

    # Ширина колонки по самому длинному содержимому: в отличие от текстовой
    # версии здесь нет причин фиксировать её — картинка не переносится.
    widths = []
    for i in range(ncol):
        # у двухстрочного заголовка считаем самую длинную строку, а не всю
        # склейку с \n — иначе колонка раздувалась вдвое
        head_len = max(len(part) for part in str(header[i]).split("\n"))
        longest = max([head_len] + [len(str(r[i])) for r in rows])
        widths.append(max(longest * CHAR_W + 2 * PAD_X, 0.5))

    table_w = sum(widths) + dot_w
    # Заголовок или подпись могут быть длиннее таблицы — тогда растягиваем по
    # ним, а разницу раздаём ВСЕМ колонкам пропорционально, не последней: иначе
    # у «ккал» появлялся пустой хвост в полтаблицы, а числа отъезжали от глаз.
    # Без учёта footer длинная строка средних уезжала за правый край картинки.
    title_w = len(title) * CHAR_W + 2 * PAD_X + 0.3   # +запас под жирный шрифт
    foot_w = len(footer) * CHAR_W * 0.87 + 2 * PAD_X if footer else 0.0
    need_w = max(title_w, foot_w)
    if need_w > table_w:
        k = (need_w - dot_w) / sum(widths)
        widths = [w * k for w in widths]
        table_w = need_w

    foot_h = 0.3 if footer else 0.0
    # Поля 0.12" по кругу; снизу под подписью хватает самой foot_h, лишний
    # отступ давал белую полосу в треть таблицы.
    MARGIN = 0.12
    total_h = TITLE_H + head_h + len(rows) * ROW_H + foot_h

    fig = plt.figure(figsize=(table_w + 2 * MARGIN, total_h + 2 * MARGIN),
                     facecolor=BG)
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, table_w + 2 * MARGIN)
    ax.set_ylim(0, total_h + 2 * MARGIN)
    ax.axis("off")

    x0, y_top = MARGIN, total_h + MARGIN

    # ---------------------------------------------------------- заголовок
    y = y_top - TITLE_H
    _gradient(ax, x0, y, table_w, TITLE_H, radius=0.07)
    # Заголовок по центру полосы: прижатый влево он висел над первой колонкой
    # и читался как её подпись, а не как название всей таблицы.
    ax.text(
        x0 + table_w / 2, y + TITLE_H / 2, title,
        color="#FFFFFF", fontsize=12, fontweight="bold",
        va="center", ha="center", zorder=3,
    )

    # ------------------------------------------------------- шапка колонок
    if show_head:
        y -= head_h
        ax.add_patch(Rectangle((x0, y), table_w, head_h, facecolor="#F2F6FB",
                               edgecolor="none", zorder=1))
        cx = x0 + dot_w
        for i, name in enumerate(header):
            # Подпись и иконку центруем по КОЛОНКЕ, а не по её краю: значения
            # выровнены вправо, и прижатый вправо заголовок висел над последней
            # цифрой, а не над столбцом. По центру шапка читается как подпись
            # ко всему столбцу.
            shift = 0.0
            if head_icons and i < len(head_icons) and head_icons[i]:
                if _put_emoji(ax, head_icons[i], cx + widths[i] / 2,
                              y + head_h - HEAD_ICON / 2 - 0.10,
                              size=HEAD_ICON):
                    shift = -HEAD_ICON / 2 - 0.04
            _cell_text(ax, cx, y + shift, widths[i], head_h, str(name), "c",
                       color=MUTED, weight="bold", size=9.5)
            cx += widths[i]

    # -------------------------------------------------------------- строки
    for ri, row in enumerate(rows):
        y -= ROW_H
        if ri % 2 == 1:
            ax.add_patch(Rectangle((x0, y), table_w, ROW_H, facecolor=ZEBRA,
                                   edgecolor="none", zorder=1))
        if icons and ri < len(icons) and icons[ri]:
            _put_emoji(ax, icons[ri], x0 + dot_w / 2, y + ROW_H / 2)
        cx = x0 + dot_w
        for i, cell in enumerate(row):
            color = TEXT
            if colors is not None and colors[ri][i]:
                color = colors[ri][i]
            _cell_text(ax, cx, y, widths[i], ROW_H, str(cell), aligns[i],
                       color=color, size=10.5)
            cx += widths[i]

    # рамка поверх зебры, чтобы линия не пряталась под подложкой
    ax.add_patch(Rectangle(
        (x0, y), table_w, y_top - TITLE_H - y,
        facecolor="none", edgecolor=BORDER, linewidth=0.9, zorder=4,
    ))

    if footer:
        ax.text(x0 + PAD_X, y - foot_h / 2, footer, color=MUTED,
                fontsize=9.5, va="center", ha="left")

    return _fig_png(fig)


async def send_table(message, title, header, rows, aligns=None, colors=None,
                     footer="", icons=None, head_icons=None,
                     caption="", reply_markup=None) -> None:
    """Отрисовать таблицу и отправить фотографией.

    Рендер уводим в поток: matplotlib блокирующий, и на таблице в 30 строк
    подвисал бы весь бот — та же причина, что у _send_chart в commands.py.

    Сигнатура повторяет render_table: параметры передаются позиционно, поэтому
    новое поле нужно добавлять В ОБА места — иначе вызов падает с TypeError
    уже в рантайме, а не при импорте.
    """
    import asyncio

    from aiogram.types import BufferedInputFile

    png = await asyncio.to_thread(
        render_table, title, header, rows, aligns, colors, footer,
        icons, head_icons
    )
    await message.answer_photo(
        BufferedInputFile(png, filename="table.png"),
        caption=caption or None,
        parse_mode="Markdown" if caption else None,
        reply_markup=reply_markup,
    )


def _cell_text(ax, x, y, w, h, text, align, color=TEXT, weight="normal",
               size=10.5) -> None:
    """Текст в ячейке. align: "l" | "r" | "c" — влево, вправо, по центру."""
    if align == "c":
        ax.text(x + w / 2, y + h / 2, text, color=color, fontsize=size,
                fontweight=weight, va="center", ha="center", zorder=3)
    elif align == "r":
        ax.text(x + w - PAD_X, y + h / 2, text, color=color, fontsize=size,
                fontweight=weight, va="center", ha="right", zorder=3)
    else:
        ax.text(x + PAD_X, y + h / 2, text, color=color, fontsize=size,
                fontweight=weight, va="center", ha="left", zorder=3)
