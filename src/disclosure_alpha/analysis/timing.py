"""Правила привязки момента публикации раскрытия к торговым дням MOEX.

Термины
-------
* ``t0`` («день события», :func:`assign_event_day`) -- первый торговый день, цена закрытия
  которого уже могла отразить сообщение:

  - публикация в торговый день **строго до** ``cutoff_time`` (по умолчанию 18:40 --
    начало аукциона закрытия основной сессии) -> этот же день;
  - публикация в торговый день в/после ``cutoff_time``, либо в выходной/праздник
    -> ближайший торговый день строго после даты публикации.

  Дневная доходность за t0 (close(t0)/close(t0-1) - 1) -- первая, в которой может быть
  реакция рынка; окно (0, k) event study отсчитывается от t0.

* «день входа по следующему открытию» (:func:`entry_day_next_open`) -- первый торговый
  день, открытие которого (``open_time``, 10:00) наступает **строго после** момента
  публикации: публикация в торговый день до 10:00 -> этот же день, иначе -> следующий
  торговый день. Используется бэктестом для входа «по цене открытия следующей сессии».

Все функции работают с наивными временными метками московского времени (как в
``events.published_at``) и календарём торговых дней ``trading_days`` (нормализованный
``DatetimeIndex``, обычно -- даты, для которых есть дневные свечи). Если подходящего
торгового дня в календаре нет (публикация после последнего дня), возвращается ``NaT``.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable, Union

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

TimeLike = Union[str, dt.time, pd.Timedelta]
DEFAULT_CUTOFF_TIME = "18:40"
DEFAULT_OPEN_TIME = "10:00"


def parse_time_of_day(value: TimeLike) -> pd.Timedelta:
    """Преобразует "HH:MM[:SS]" / ``datetime.time`` / ``Timedelta`` в смещение от полуночи."""
    if isinstance(value, pd.Timedelta):
        return value
    if isinstance(value, dt.time):
        return pd.Timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)
    parts = str(value).strip().split(":")
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"Неверный формат времени: {value!r}, ожидается 'HH:MM[:SS]'")
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    return pd.Timedelta(hours=hours, minutes=minutes, seconds=seconds)


def as_datetime64(values: Iterable | pd.Series, index=None) -> pd.Series:
    """Приводит даты/строки/``datetime`` к ``Series`` с dtype ``datetime64[ns]`` (единая точность)."""
    if isinstance(values, pd.Series):
        s = values
    else:
        s = pd.Series(list(values) if not isinstance(values, (np.ndarray, pd.Index)) else values, index=index)
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.astype("datetime64[ns]")
    try:
        s = pd.to_datetime(s, errors="raise")
    except (ValueError, TypeError):  # строки разных форматов / мусор -> поэлементный разбор, ошибки -> NaT
        s = pd.to_datetime(s, errors="coerce", format="mixed")
    return s.astype("datetime64[ns]")


def normalize_trading_days(trading_days: Iterable) -> pd.DatetimeIndex:
    """Нормализованный (00:00), отсортированный, уникальный календарь торговых дней."""
    idx = pd.DatetimeIndex(pd.to_datetime(pd.Index(trading_days), errors="coerce"))
    idx = idx.dropna().astype("datetime64[ns]").normalize().unique().sort_values()
    return pd.DatetimeIndex(idx)


def _session_positions(published: pd.Series, trading_days: pd.DatetimeIndex, same_day_before: pd.Timedelta) -> np.ndarray:
    """Позиции в ``trading_days`` первого подходящего дня (векторно).

    День публикации подходит, если он торговый и время публикации строго меньше
    ``same_day_before``; иначе берётся первый торговый день строго после даты публикации.
    Возвращает массив позиций; значение ``len(trading_days)`` означает «нет такого дня».
    """
    n = len(trading_days)
    ts = as_datetime64(published)
    dates = ts.dt.normalize()
    tod = ts - dates
    dates_np = dates.to_numpy(dtype="datetime64[ns]")
    not_nat = ~np.isnat(dates_np)
    safe_dates = np.where(not_nat, dates_np, trading_days.to_numpy()[0] if n else np.datetime64("2000-01-01", "ns"))

    pos_left = trading_days.searchsorted(safe_dates, side="left")
    pos_right = trading_days.searchsorted(safe_dates, side="right")
    clipped = np.minimum(pos_left, max(n - 1, 0))
    is_trading = (pos_left < n) & (trading_days.to_numpy()[clipped] == safe_dates) if n else np.zeros(len(ts), bool)
    tod_np = tod.to_numpy(dtype="timedelta64[ns]")
    before = np.where(not_nat, tod_np < np.timedelta64(same_day_before.to_timedelta64()), False)
    pos = np.where(is_trading & before, pos_left, pos_right)
    pos = np.where(not_nat, pos, n)
    return pos.astype(int)


def _positions_to_days(pos: np.ndarray, trading_days: pd.DatetimeIndex, index, name: str) -> pd.Series:
    n = len(trading_days)
    days = trading_days.to_numpy()
    out = np.full(len(pos), np.datetime64("NaT", "ns"), dtype="datetime64[ns]")
    ok = pos < n
    out[ok] = days[pos[ok]]
    return pd.Series(out, index=index, name=name)


def assign_event_days(
    events: pd.DataFrame | pd.Series,
    trading_days: Iterable,
    cutoff_time: TimeLike = DEFAULT_CUTOFF_TIME,
    column: str = "published_at",
) -> pd.Series:
    """Векторный вариант :func:`assign_event_day`.

    ``events`` -- DataFrame с колонкой ``column`` или Series временных меток.
    Возвращает Series ``t0`` (datetime64[ns], NaT если торгового дня нет), индекс как у входа.
    """
    published = events[column] if isinstance(events, pd.DataFrame) else events
    td = normalize_trading_days(trading_days)
    pos = _session_positions(published, td, parse_time_of_day(cutoff_time))
    return _positions_to_days(pos, td, published.index, "t0")


def assign_event_day(published_at, trading_days: Iterable, cutoff_time: TimeLike = DEFAULT_CUTOFF_TIME) -> pd.Timestamp:
    """День события t0 для одной временной метки (правила -- в докстринге модуля)."""
    res = assign_event_days(pd.Series([published_at]), trading_days, cutoff_time)
    return pd.Timestamp(res.iloc[0])


def entry_days_next_open(
    events: pd.DataFrame | pd.Series,
    trading_days: Iterable,
    open_time: TimeLike = DEFAULT_OPEN_TIME,
    column: str = "published_at",
) -> pd.Series:
    """Векторный вариант :func:`entry_day_next_open` (Series ``entry_day``)."""
    published = events[column] if isinstance(events, pd.DataFrame) else events
    td = normalize_trading_days(trading_days)
    pos = _session_positions(published, td, parse_time_of_day(open_time))
    return _positions_to_days(pos, td, published.index, "entry_day")


def entry_day_next_open(published_at, trading_days: Iterable, open_time: TimeLike = DEFAULT_OPEN_TIME) -> pd.Timestamp:
    """Первый торговый день, открытие которого наступает строго после публикации.

    Публикация в торговый день до ``open_time`` (10:00) -> этот день; иначе -> следующий
    торговый день строго после даты публикации.
    """
    res = entry_days_next_open(pd.Series([published_at]), trading_days, open_time)
    return pd.Timestamp(res.iloc[0])


def event_day_positions(t0: pd.Series, trading_days: pd.DatetimeIndex) -> np.ndarray:
    """Позиции дней ``t0`` в календаре (``-1`` для NaT / отсутствующих дней)."""
    td = normalize_trading_days(trading_days)
    return td.get_indexer(pd.DatetimeIndex(as_datetime64(t0)))
