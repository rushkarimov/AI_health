"""Графики метрик — PNG в память, без записи на диск.

matplotlib в headless-режиме: бэкенд Agg выбирается до импорта pyplot, иначе
он попытается найти дисплей и упадёт в контейнере.

Единицы у метрик разные (ккал, кг, часы, шаги), поэтому несколько метрик на
одном графике рисуем только когда они сопоставимы — иначе вес в 80 кг
превратится в плоскую линию рядом с 2000 ккал.
"""
from __future__ import annotations

import io
import logging
from datetime import date
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from app.db import repo  # noqa: E402

log = logging.getLogger(__name__)

# Светлая палитра. Тёмный фон читался хуже на свету и в светлой теме
# Телеграма выглядел чужеродным прямоугольником.
#
# Цвета — те же, что в таблицах-картинках (services/tables.py) и в презентации
# ML-команды: график и таблица под ним должны читаться одним продуктом, а не
# двумя разными инструментами. Заливка под линиями идёт градиентом синий →
# фиолетовый, как в шапках таблиц.
BG = "#FFFFFF"
FG = "#0B1020"
MUTED = "#5A6472"
GRID = "#EAEEF3"
ACCENT = "#0D8BFF"     # синий — все линии и столбцы
VIOLET = "#9641FF"     # вторая точка градиента
# Зелёного и оранжевого в графиках больше нет: раскраска по знаку («профицит
# красный, дефицит зелёный») дробила сетку на разноцветные панели. Единый
# синий → фиолетовый читается как один инструмент, а знак виден по числу.

plt.rcParams.update({
    "figure.facecolor": BG,
    "axes.facecolor": BG,
    "axes.edgecolor": GRID,
    "axes.labelcolor": MUTED,
    "text.color": FG,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "grid.color": GRID,
    "font.size": 11,
    "font.family": "DejaVu Sans",
    "figure.dpi": 140,
    "axes.titleweight": "600",
})


def _style(ax, dates: Sequence[Any] | None = None) -> None:
    """Сетка только горизонтальная, рамка снята, даты без дублей.

    Вертикальные линии сетки на графике из семи точек только шумят, а
    автоматический локатор дат при малом числе точек ставил метки дважды
    на один день — отсюда явный DayLocator по фактическим датам.
    """
    ax.grid(True, axis="y", alpha=0.9, linewidth=0.8, color=GRID)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    if dates is not None and len(dates) > 1:
        # не больше семи подписей, иначе на телефоне они наезжают друг на друга
        step = max(1, len(dates) // 7)
        ax.set_xticks(list(dates)[::step])


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=BG, edgecolor="none")
    plt.close(fig)
    return buf.getvalue()


def _grad_fill(ax, xs, ys, c1: str = ACCENT, c2: str = VIOLET,
               base: float | None = None) -> None:
    """Заливка под линией градиентом, как в шапках таблиц.

    Плоская fill_between с alpha давала ровное пятно; вертикальный градиент,
    затухающий к низу, читается как объём и не спорит с линией.

    Механика: imshow растягивает картинку 256×1 на всю область, а fill_between
    той же формы служит маской отсечения — иначе прямоугольник закрыл бы сетку
    под кривой.
    """
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    if len(xs) < 2:
        return

    if base is None:
        base = ax.get_ylim()[0]

    # Пределы запоминаем и возвращаем: imshow участвует в автомасштабе, а по X
    # у нас даты в числах matplotlib против единиц метрики по Y — от такого
    # соотношения фигуру раздувало до немыслимой ширины.
    xlim, ylim = ax.get_xlim(), ax.get_ylim()

    cmap = LinearSegmentedColormap.from_list("fill", [c1, c2])
    grad = np.linspace(0, 1, 256).reshape(-1, 1)
    im = ax.imshow(
        grad, aspect="auto", origin="upper", cmap=cmap,
        alpha=0.30, zorder=1,
        extent=(mdates.date2num(xs[0]), mdates.date2num(xs[-1]),
                base, max(ys)),
    )
    clip = ax.fill_between(xs, ys, base, facecolor="none", edgecolor="none")
    im.set_clip_path(clip.get_paths()[0], transform=ax.transData)

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)


def _grad_bar(ax, bar, c1: str, c2: str) -> None:
    """Столбец с вертикальным градиентом.

    Заливаем каждый столбец своим imshow по габаритам патча: у столбцов разная
    высота и знак, поэтому одна картинка на всю область раскрасила бы их
    одинаково, без связи со значением.

    Границы осей запоминаем и возвращаем: imshow добавляет себя в автомасштаб,
    а по оси X у нас даты в числах matplotlib (~740000) против ккал по Y —
    от такого соотношения фигура растягивалась до 16000 px по ширине.
    """
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    xlim, ylim = ax.get_xlim(), ax.get_ylim()

    x, y = bar.get_xy()
    w, h = bar.get_width(), bar.get_height()
    cmap = LinearSegmentedColormap.from_list("bar", [c1, c2])
    # у отрицательных столбцов h < 0: extent нужен по возрастанию, иначе
    # imshow рисует картинку вверх ногами и градиент идёт не туда
    lo, hi = sorted((y, y + h))
    grad = np.linspace(0, 1, 128).reshape(-1, 1)
    if h < 0:
        grad = grad[::-1]
    im = ax.imshow(grad, aspect="auto", origin="upper", cmap=cmap,
                   extent=(x, x + w, lo, hi), zorder=3, alpha=0.9)
    im.set_clip_path(bar)

    ax.set_xlim(xlim)
    ax.set_ylim(ylim)


def _series(rows: Sequence[dict[str, Any]], col: str) -> tuple[list, list]:
    """Точки без пропусков: matplotlib рисует None как разрыв, а нам нужна
    непрерывная линия по тем дням, где данные есть."""
    xs, ys = [], []
    for r in rows:
        v = r.get(col)
        if v is not None:
            xs.append(r["day"])
            ys.append(float(v))
    return xs, ys


def metric_chart(rows: Sequence[dict[str, Any]], metric: str) -> bytes | None:
    """График одной метрики. None — если данных меньше двух точек."""
    col, label, unit = repo.METRICS[metric]
    xs, ys = _series(rows, col)
    if len(xs) < 2:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(xs, ys, marker="o", markersize=4, linewidth=2.4, color=ACCENT,
            solid_capstyle="round", zorder=3)
    # пределы фиксируем до заливки: _grad_fill читает ylim, а после imshow
    # автомасштаб уехал бы по картинке, а не по данным
    ax.set_ylim(min(ys) * 0.97, max(ys) * 1.03)
    _grad_fill(ax, xs, ys, base=min(ys) * 0.97)

    # среднее — чтобы глазом отделить выбросы от тренда
    avg = sum(ys) / len(ys)
    ax.axhline(avg, linestyle="--", linewidth=1, color=MUTED, alpha=0.8)
    ax.annotate(
        f"среднее {avg:.1f}".rstrip("0").rstrip("."),
        xy=(xs[0], avg), xytext=(0, 5), textcoords="offset points",
        fontsize=9, color=MUTED,
    )

    ax.set_title(f"{label}{f', {unit}' if unit else ''}", pad=12)
    _style(ax)
    fig.autofmt_xdate()
    return _png(fig)


def balance_chart(rows: Sequence[dict[str, Any]]) -> bytes | None:
    """Съедено против потраченного плюс баланс столбиками.

    Самый информативный график: по нему видно, откуда берётся динамика веса.
    """
    xs_e, ys_e = _series(rows, "kcal_eaten")
    xs_b, ys_b = _series(rows, "kcal_burned")
    if len(xs_e) < 2 and len(xs_b) < 2:
        return None

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(9, 6), sharex=True, gridspec_kw={"height_ratios": [2, 1]}
    )

    # заливку здесь не делаем: две линии друг под другом с заливкой сливаются
    if xs_e:
        top.plot(xs_e, ys_e, marker="o", markersize=4, linewidth=2, color=ACCENT, label="съедено")
    if xs_b:
        top.plot(xs_b, ys_b, marker="o", markersize=4, linewidth=2, color=VIOLET, label="потрачено")
    top.set_title("Калории: съедено и потрачено", pad=12)
    top.legend(facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)
    _style(top)

    xs_bal, ys_bal = _series(rows, "balance")
    if xs_bal:
        # Столбцы градиентом: профицит оранжевый → красноватый, дефицит
        # зелёный → синий. Каждый столбец заливается отдельно — общий imshow
        # на всю область давал бы одинаковый цвет независимо от высоты.
        bars = bottom.bar(xs_bal, ys_bal, color="none", width=0.7, zorder=3)
        for bar, v in zip(bars, ys_bal):
            _grad_bar(bottom, bar, ACCENT, VIOLET)
        bottom.axhline(0, linewidth=1, color=FG, alpha=0.4)
        bottom.set_title("Баланс (съедено − потрачено)", fontsize=10, pad=8)
    _style(bottom)

    fig.autofmt_xdate()
    return _png(fig)


def weight_chart(rows: Sequence[dict[str, Any]]) -> bytes | None:
    """Вес и процент жира на двух осях: вместе они читаются лучше, чем
    по отдельности — вес может стоять, а состав тела меняться."""
    xs_w, ys_w = _series(rows, "weight_kg")
    if len(xs_w) < 2:
        return None

    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(xs_w, ys_w, marker="o", markersize=4, linewidth=2.4, color=ACCENT,
            label="вес, кг", solid_capstyle="round", zorder=3)
    ax.set_ylabel("вес, кг")
    ax.set_ylim(min(ys_w) * 0.985, max(ys_w) * 1.015)
    _grad_fill(ax, xs_w, ys_w, base=min(ys_w) * 0.985)
    _style(ax)

    xs_f, ys_f = _series(rows, "body_fat_pct")
    if len(xs_f) >= 2:
        ax2 = ax.twinx()
        ax2.plot(xs_f, ys_f, marker="s", markersize=3, linewidth=1.6,
                 color=VIOLET, label="жир, %")
        ax2.set_ylabel("жир, %")
        ax2.spines["top"].set_visible(False)
        ax2.grid(False)
        lines = ax.get_lines() + ax2.get_lines()
        ax.legend(lines, [l.get_label() for l in lines],
                  facecolor=BG, edgecolor=GRID, labelcolor=FG, fontsize=9)

    ax.set_title("Вес и состав тела", pad=12)
    fig.autofmt_xdate()
    return _png(fig)


def overview_chart(rows: Sequence[dict[str, Any]]) -> bytes | None:
    """Сводная панель 2x2: разница калорий, вес, дистанция, сон.

    Те же метрики, что в таблице под кнопкой «Сегодня» — чтобы картинка
    и цифры не расходились. Раньше здесь были съеденные калории и шаги:
    съеденное без расхода мало говорит, а шаги дублируют дистанцию.
    """
    panels = [
        ("balance", "Разница калорий", ACCENT, True),
        ("weight_kg", "Вес, кг", ACCENT, False),
        ("distance_km", "Дистанция, км", ACCENT, False),
        ("sleep_hours", "Сон, ч", ACCENT, False),
    ]
    drawable = [p for p in panels if len(_series(rows, p[0])[0]) >= 2]
    if not drawable:
        return None

    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    for ax, (col, title, color, zero) in zip(axes.flat, drawable):
        xs, ys = _series(rows, col)
        # точки только на концах: на семи днях маркеры в каждой вершине
        # превращают линию в пунктир из кружков
        ax.plot(xs, ys, linewidth=2.6, color=color, solid_capstyle="round")
        ax.plot(xs[-1:], ys[-1:], marker="o", markersize=7, color=color,
                markeredgecolor=BG, markeredgewidth=2, zorder=5)
        if zero and min(ys) < 0 < max(ys):
            # Пунктир по нулю оставляем — он показывает границу дефицита и
            # профицита. А вот двухцветная заливка убрана: панель выбивалась
            # из сетки, где остальные три идут брендовым синий → фиолетовый.
            # Знак и так виден по крупному числу в заголовке, оно красится.
            ax.axhline(0, color=MUTED, linewidth=1, linestyle="--", alpha=0.6)
        # база заливки — низ оси: у веса разброс 92-95 кг, и заливка
        # от min*0.97 визуально гасила колебания
        _grad_fill(ax, xs, ys, c1=color, c2=VIOLET)
        # последнее значение крупно в заголовке — главное, что хочет знать
        # человек, не листая ось
        last = ys[-1]
        digits = 1 if abs(last) < 100 else 0
        ax.set_title(title, fontsize=10, pad=22, color=MUTED, loc="left",
                     fontweight="normal")
        ax.text(0, 1.02, f"{last:+,.{digits}f}" if zero else f"{last:,.{digits}f}",
                transform=ax.transAxes, fontsize=17, fontweight="bold",
                color=FG,
                va="bottom", ha="left")
        _style(ax, xs)
        ax.tick_params(labelsize=8, length=0)

    # лишние ячейки прячем, иначе висят пустые рамки
    for ax in list(axes.flat)[len(drawable):]:
        ax.set_visible(False)

    fig.suptitle("Калории, вес, активность и сон", fontsize=14, fontweight="bold",
                 color=FG, y=0.99)
    # autofmt_xdate() не вызываем: он прячет подписи дат у верхнего ряда,
    # считая оси общими. У панелей разные периоды (сон приходит реже), поэтому
    # даты нужны под каждой.
    fig.tight_layout(rect=(0, 0, 1, 0.96), h_pad=4, w_pad=3)
    return _png(fig)


def psy_chart(rows: Sequence[dict[str, Any]]) -> bytes | None:
    """Сетка для раздела «Психолог»: сон, пульс покоя, ВСР, шаги.

    Панели, по которым нет данных, не рисуем — пустая рамка с прочерками
    хуже отсутствия панели. Если совсем нечего показать, возвращаем None
    и хендлер просто не отправляет картинку.
    """
    panels = [
        ("sleep_hours", "Сон, ч", ACCENT),
        ("resting_hr", "Пульс покоя", ACCENT),
        ("hrv_ms", "ВСР, мс", ACCENT),
        ("steps", "Шаги", ACCENT),
    ]
    drawable = [p for p in panels if len(_series(rows, p[0])[0]) >= 2]
    if not drawable:
        return None

    n = len(drawable)
    cols = 2 if n > 1 else 1
    rows_n = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows_n, cols, figsize=(11, 3.6 * rows_n), squeeze=False)
    flat = axes.flat

    for ax, (col, title, color) in zip(flat, drawable):
        xs, ys = _series(rows, col)
        ax.plot(xs, ys, linewidth=2.6, color=color, solid_capstyle="round")
        ax.plot(xs[-1:], ys[-1:], marker="o", markersize=7, color=color,
                markeredgecolor=BG, markeredgewidth=2, zorder=5)
        _grad_fill(ax, xs, ys, c1=color, c2=VIOLET)
        last = ys[-1]
        digits = 1 if abs(last) < 100 else 0
        ax.set_title(title, fontsize=10, pad=22, color=MUTED, loc="left")
        ax.text(0, 1.02, f"{last:,.{digits}f}", transform=ax.transAxes,
                fontsize=17, fontweight="bold", color=FG, va="bottom", ha="left")
        _style(ax, xs)
        ax.tick_params(labelsize=8, length=0)

    for ax in list(flat)[n:]:
        ax.set_visible(False)

    fig.suptitle("Сон, пульс и восстановление", fontsize=14, fontweight="bold",
                 color=FG, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.95), h_pad=4, w_pad=3)
    return _png(fig)


async def build(user_id: int, kind: str, date_from: date, date_to: date) -> bytes | None:
    """Точка входа для хендлеров: тянет данные и рисует нужный график."""
    rows = await repo.period_full(user_id, date_from, date_to)
    if not rows:
        return None
    if kind == "balance":
        return balance_chart(rows)
    if kind == "weight":
        return weight_chart(rows)
    if kind == "overview":
        return overview_chart(rows)
    if kind == "psy":
        return psy_chart(rows)
    if kind in repo.METRICS:
        return metric_chart(rows, kind)
    log.warning("Неизвестный тип графика: %s", kind)
    return None
