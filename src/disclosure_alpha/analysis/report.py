"""Отчёты: Markdown с таблицами (без зависимости от ``tabulate``) и простые графики.

Графики строятся matplotlib с backend ``Agg`` (без дисплея); подписи осей -- по-русски.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import numbers
from pathlib import Path
from typing import Mapping, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

log = logging.getLogger(__name__)

# Фиксированный порядок категориальных цветов (проверенная на дальтонизм палитра).
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DIRECTION_LABELS = {1: "покупка (+1)", -1: "продажа (-1)", 0: "нейтрально (0)", "signed": "по направлению сделки"}


# --------------------------------------------------------------------------------- markdown
def format_value(value, float_digits: int = 4) -> str:
    """Значение ячейки -> строка: float с округлением, даты ISO, NaN -> пусто, '|' экранируется."""
    if value is None:
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "True" if value else "False"
    if isinstance(value, (float, np.floating)):
        v = float(value)
        if math.isnan(v):
            return ""
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        s = f"{v:.{float_digits}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s not in ("-0", "") else "0"
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, (pd.Timestamp, dt.datetime)):
        if pd.isna(value):
            return ""
        ts = pd.Timestamp(value)
        return ts.strftime("%Y-%m-%d") if ts == ts.normalize() else ts.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (pd.Timedelta, dt.timedelta)):
        return str(value)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).replace("\n", " ").replace("|", "\\|")


def df_to_markdown(df: pd.DataFrame, float_digits: int = 4, max_rows: int | None = None, index: bool = False) -> str:
    """Таблица DataFrame в формате Markdown (GFM); числа выравниваются по правому краю."""
    if df is None or len(df.columns) == 0:
        return "_нет данных_"
    frame = df.reset_index() if index else df
    truncated = max_rows is not None and len(frame) > max_rows
    if truncated:
        frame = frame.head(max_rows)
    headers = [format_value(c, float_digits) or " " for c in frame.columns]
    numeric = [pd.api.types.is_numeric_dtype(frame[c]) and not pd.api.types.is_bool_dtype(frame[c]) for c in frame.columns]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---:" if num else "---" for num in numeric) + "|"]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(format_value(v, float_digits) for v in row) + " |")
    if not len(frame):
        lines.append("| " + " | ".join("" for _ in headers) + " |")
    text = "\n".join(lines)
    if truncated:
        text += f"\n\n_показано {max_rows} строк из {len(df)}_"
    return text


def mapping_to_markdown(data: Mapping, float_digits: int = 4) -> str:
    """dict -> таблица «параметр / значение»."""
    rows = [{"параметр": str(k), "значение": format_value(v, float_digits) if not isinstance(v, (dict, list, tuple)) else str(v)} for k, v in data.items()]
    return df_to_markdown(pd.DataFrame(rows, columns=["параметр", "значение"]), float_digits)


def write_markdown_report(
    path: str | Path,
    sections: Sequence[tuple[str, pd.DataFrame | Mapping | str]],
    title: str | None = None,
    float_digits: int = 4,
    max_rows: int | None = None,
) -> Path:
    """Пишет Markdown-отчёт: каждая секция -- заголовок второго уровня и таблица/текст.

    Элемент ``sections``: (название, DataFrame | dict | str). Возвращает путь к файлу.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")
    for name, content in sections:
        parts.append(f"## {name}\n")
        if isinstance(content, pd.DataFrame):
            parts.append(df_to_markdown(content, float_digits, max_rows) + "\n")
        elif isinstance(content, pd.Series):
            parts.append(df_to_markdown(content.reset_index(), float_digits, max_rows) + "\n")
        elif isinstance(content, Mapping):
            parts.append(mapping_to_markdown(content, float_digits) + "\n")
        else:
            parts.append(str(content).rstrip() + "\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    log.info("отчёт записан: %s (%d секций)", path, len(sections))
    return path


# ---------------------------------------------------------------------------------- графики
def _series_label(category, direction) -> str:
    d = DIRECTION_LABELS.get(direction, str(direction))
    return f"{category}, {d}"


def plot_car_paths(path_df: pd.DataFrame, out_png: str | Path, title: str | None = None, value_col: str = "mean_car") -> Path:
    """Кривые среднего CAR по относительным дням (результат ``average_ar_path``), по группам."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    keys = [c for c in ("category", "direction") if c in path_df.columns]
    fig, ax = plt.subplots(figsize=(9, 5))
    groups = list(path_df.groupby(keys, sort=True)) if keys else [((), path_df)]
    for i, (key, g) in enumerate(groups):
        key = key if isinstance(key, tuple) else (key,)
        label = _series_label(*key) if len(key) == 2 else (str(key[0]) if key else value_col)
        color = PALETTE[i % len(PALETTE)]
        style = "-" if i < len(PALETTE) else "--"
        g = g.sort_values("rel_day")
        ax.plot(g["rel_day"], g[value_col], style, color=color, linewidth=2, label=label)
    ax.axvline(0, color="#888888", linestyle="--", linewidth=1)
    ax.axhline(0, color="#888888", linewidth=1)
    ax.set_xlabel("Торговые дни относительно события (t0 = 0)")
    ax.set_ylabel("Средняя кумулятивная аномальная доходность (CAR)")
    ax.grid(True, alpha=0.3)
    if title:
        ax.set_title(title)
    if len(groups) >= 2:
        ax.legend(loc="best", fontsize=9)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return out_png


def plot_equity(daily_df: pd.DataFrame, out_png: str | Path, title: str | None = None) -> Path:
    """Кривая капитала стратегии (и бенчмарка, если есть ``index_equity``) + просадка."""
    out_png = Path(out_png)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.to_datetime(daily_df["date"])
    eq = daily_df["equity"].to_numpy(dtype=float)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(dates, eq, color=PALETTE[0], linewidth=2, label="Стратегия")
    if "index_equity" in daily_df.columns and daily_df["index_equity"].notna().any():
        ax1.plot(dates, daily_df["index_equity"].to_numpy(dtype=float), color=PALETTE[1], linewidth=2, label="Индекс (бенчмарк)")
        ax1.legend(loc="best", fontsize=9)
    ax1.set_ylabel("Капитал, руб.")
    ax1.grid(True, alpha=0.3)
    if title:
        ax1.set_title(title)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    ax2.fill_between(dates, dd, 0.0, color=PALETTE[7], alpha=0.4, linewidth=0)
    ax2.plot(dates, dd, color=PALETTE[7], linewidth=1)
    ax2.set_ylabel("Просадка")
    ax2.set_xlabel("Дата")
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    return out_png
