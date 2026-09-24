"""Доходности и аномальные доходности (AR) по панели дневных цен.

Соглашения
----------
* «Широкая» таблица (:func:`to_wide`): индекс -- торговые дни (datetime64[ns]), колонки --
  ``secid``; пропуски (приостановка торгов, ещё/уже не торгуется) остаются ``NaN``.
* Дневная доходность r_t = P_t / P_{t-1} - 1. При ``fill_gaps=True`` (по умолчанию) в
  знаменателе берётся последняя доступная цена, т.е. движение за период остановки торгов
  относится к первому дню возобновления; при ``fill_gaps=False`` такая доходность -- NaN.
* Модель «market adjusted»: AR_t = r_t - r_m,t.
* Рыночная модель: AR_t = r_t - (alpha + beta * r_m,t), alpha/beta -- OLS по окну оценки
  (относительные торговые дни ``est_window``, по умолчанию (-250, -30)) отдельно для
  каждого события. Если наблюдений меньше ``min_obs`` (или дисперсия рынка нулевая),
  используется market adjusted (alpha=0, beta=1) с пометкой ``model="market_adjusted"``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from disclosure_alpha.analysis.timing import as_datetime64, normalize_trading_days

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- панель цен
def to_wide(prices: pd.DataFrame, column: str = "close") -> pd.DataFrame:
    """Long -> wide: индекс ``date`` (datetime64[ns], по возрастанию), колонки ``secid`` (по алфавиту).

    Forward-fill НЕ применяется: пропуски остаются NaN. Дубликаты (date, secid) -- берётся последний.
    """
    if column not in prices.columns:
        raise KeyError(f"В prices нет колонки {column!r}")
    df = prices[["date", "secid", column]].copy()
    df["date"] = as_datetime64(df["date"]).dt.normalize()
    df["secid"] = df["secid"].astype(str)
    df = df.dropna(subset=["date"])
    dup = df.duplicated(["date", "secid"], keep="last")
    if dup.any():
        log.warning("to_wide: %d дубликатов (date, secid) -- оставлен последний", int(dup.sum()))
        df = df[~dup]
    wide = df.pivot(index="date", columns="secid", values=column).astype(float)
    wide = wide.sort_index()
    wide = wide.reindex(columns=sorted(wide.columns))
    wide.index = pd.DatetimeIndex(wide.index).astype("datetime64[ns]")
    wide.index.name = "date"
    wide.columns.name = "secid"
    return wide


def trading_calendar(prices: pd.DataFrame | None = None, index: pd.DataFrame | None = None) -> pd.DatetimeIndex:
    """Календарь торговых дней: объединение дат котировок и дат бенчмарка."""
    parts = []
    if prices is not None and len(prices):
        parts.append(as_datetime64(prices["date"]).dropna().dt.normalize().unique())
    if index is not None and len(index):
        parts.append(as_datetime64(index["date"]).dropna().dt.normalize().unique())
    if not parts:
        return pd.DatetimeIndex([], dtype="datetime64[ns]")
    return normalize_trading_days(np.concatenate([np.asarray(p, dtype="datetime64[ns]") for p in parts]))


def index_series(index: pd.DataFrame, calendar: pd.DatetimeIndex | None = None, column: str = "close") -> pd.Series:
    """Цены бенчмарка как Series по календарю (пропуски заполняются последней ценой)."""
    df = index[["date", column]].copy()
    df["date"] = as_datetime64(df["date"]).dt.normalize()
    s = df.dropna(subset=["date"]).drop_duplicates("date", keep="last").set_index("date")[column].astype(float).sort_index()
    s.index = pd.DatetimeIndex(s.index).astype("datetime64[ns]")
    if calendar is not None:
        s = s.reindex(calendar).ffill()
    s.name = "mkt_close"
    return s


def index_returns(index: pd.DataFrame, calendar: pd.DatetimeIndex | None = None) -> pd.Series:
    """Дневные простые доходности бенчмарка (Series ``mkt_ret``)."""
    s = index_series(index, calendar)
    r = s / s.shift(1) - 1.0
    r.name = "mkt_ret"
    return r


# ------------------------------------------------------------------------------ доходности
def simple_returns(wide: pd.DataFrame | pd.Series, fill_gaps: bool = True) -> pd.DataFrame | pd.Series:
    """Простые дневные доходности P_t/P_{t-1} - 1 (см. докстринг модуля о пропусках)."""
    prev = wide.ffill().shift(1) if fill_gaps else wide.shift(1)
    return wide / prev - 1.0


def log_returns(wide: pd.DataFrame | pd.Series, fill_gaps: bool = True) -> pd.DataFrame | pd.Series:
    """Логарифмические дневные доходности ln(P_t/P_{t-1})."""
    prev = wide.ffill().shift(1) if fill_gaps else wide.shift(1)
    return np.log(wide / prev)


def market_adjusted_ar(ret: pd.DataFrame, mkt_ret: pd.Series) -> pd.DataFrame:
    """AR = r - r_m (широкая таблица доходностей минус доходность рынка по датам)."""
    m = mkt_ret.reindex(ret.index)
    return ret.sub(m, axis=0)


# ------------------------------------------------------------------- окна вокруг событий
def gather_event_window(
    wide: pd.DataFrame | pd.Series,
    secids: Sequence[str] | None,
    t0: Iterable,
    start: int,
    end: int,
    event_ids: Sequence | None = None,
) -> pd.DataFrame:
    """Матрица «событие x относительный день» значений ``wide`` вокруг дней ``t0``.

    Относительный день k -- k-й торговый день (строка ``wide``) после t0 (k<0 -- до).
    Вне диапазона данных / для неизвестных secid / NaT -> NaN. Для Series (например,
    доходность индекса) ``secids`` игнорируется.
    """
    if isinstance(wide, pd.Series):
        frame = wide.to_frame(name=wide.name or "value")
        secids = [frame.columns[0]] * len(pd.Index(t0))
    else:
        frame = wide
    idx = pd.DatetimeIndex(frame.index)
    arr = frame.to_numpy(dtype=float)
    n_days, n_sec = arr.shape
    col_map = {s: i for i, s in enumerate(frame.columns)}
    cols = np.array([col_map.get(s, -1) for s in secids], dtype=int)
    t0_idx = pd.DatetimeIndex(as_datetime64(pd.Series(list(t0)) if not isinstance(t0, pd.Series) else t0))
    rows0 = idx.get_indexer(t0_idx)
    offsets = np.arange(start, end + 1)
    rows = rows0[:, None] + offsets[None, :]
    valid = (rows0[:, None] >= 0) & (cols[:, None] >= 0) & (rows >= 0) & (rows < n_days)
    if n_days == 0 or n_sec == 0:
        out = np.full(rows.shape, np.nan)
    else:
        rows_c = np.clip(rows, 0, n_days - 1)
        cols_c = np.broadcast_to(np.clip(cols, 0, n_sec - 1)[:, None], rows.shape)
        out = np.where(valid, arr[rows_c, cols_c], np.nan)
    if event_ids is None:
        event_ids = pd.RangeIndex(len(cols))
    res = pd.DataFrame(out, index=pd.Index(event_ids, name="event_id"), columns=offsets)
    res.columns.name = "rel_day"
    return res


def _events_t0_frame(events_t0) -> pd.DataFrame:
    """Приводит описание событий к DataFrame с колонками event_id, secid, t0."""
    if isinstance(events_t0, pd.DataFrame):
        df = events_t0.copy()
        if "event_id" not in df.columns:
            df["event_id"] = df.index.astype(str)
        missing = {"secid", "t0"} - set(df.columns)
        if missing:
            raise KeyError(f"events_t0: нет колонок {sorted(missing)}")
        df = df[["event_id", "secid", "t0"]]
    elif isinstance(events_t0, (pd.Series, Mapping)):
        s = pd.Series(dict(events_t0)) if isinstance(events_t0, Mapping) else events_t0
        df = pd.DataFrame({"event_id": [str(k) for k in s.index], "secid": list(s.index), "t0": list(s.values)})
    else:
        raise TypeError("events_t0: ожидается DataFrame(event_id, secid, t0), Series или dict secid->t0")
    df = df.reset_index(drop=True)
    df["t0"] = as_datetime64(df["t0"])
    df["secid"] = df["secid"].astype(str)
    return df


@dataclass
class MarketModelResult:
    """Результат :func:`market_model_ar`.

    ar / raw / mkt -- матрицы «событие x относительный день» (индекс event_id, колонки rel_day);
    params -- по событию: secid, t0, alpha, beta, sigma (ст. откл. остатков), n_obs, model.
    """

    ar: pd.DataFrame
    raw: pd.DataFrame
    mkt: pd.DataFrame
    params: pd.DataFrame


def market_model_ar(
    ret: pd.DataFrame,
    mkt_ret: pd.Series,
    events_t0,
    est_window: tuple[int, int] = (-250, -30),
    min_obs: int = 100,
    event_window: tuple[int, int] = (-20, 60),
) -> MarketModelResult:
    """Аномальные доходности по рыночной модели, отдельная OLS-оценка на каждое событие.

    ``events_t0`` -- DataFrame(event_id, secid, t0), либо Series/dict ``secid -> t0`` (по
    одному событию на бумагу). ``ret`` -- широкая таблица дневных доходностей, ``mkt_ret`` --
    доходность рынка по тем же датам. Окна -- в относительных торговых днях (см. модуль).
    """
    ev = _events_t0_frame(events_t0)
    mkt = mkt_ret.reindex(ret.index)
    es, ee = est_window
    ws, we = event_window
    if es > ee or ws > we:
        raise ValueError("Окна должны быть вида (start <= end)")

    r_est = gather_event_window(ret, ev["secid"], ev["t0"], es, ee, ev["event_id"]).to_numpy()
    m_est = gather_event_window(mkt, None, ev["t0"], es, ee, ev["event_id"]).to_numpy()
    valid = np.isfinite(r_est) & np.isfinite(m_est)
    n_obs = valid.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r0 = np.where(valid, r_est, 0.0)
        m0 = np.where(valid, m_est, 0.0)
        mean_r = r0.sum(1) / n_obs
        mean_m = m0.sum(1) / n_obs
        dr = np.where(valid, r_est - mean_r[:, None], 0.0)
        dm = np.where(valid, m_est - mean_m[:, None], 0.0)
        var_m = (dm * dm).sum(1)
        beta = (dr * dm).sum(1) / var_m
        alpha = mean_r - beta * mean_m
        resid = np.where(valid, r_est - alpha[:, None] - beta[:, None] * m_est, 0.0)
        sigma = np.sqrt((resid * resid).sum(1) / np.maximum(n_obs - 2, 1))
    fallback = (n_obs < min_obs) | ~np.isfinite(beta) | ~np.isfinite(alpha) | (var_m <= 0)
    alpha = np.where(fallback, 0.0, alpha)
    beta = np.where(fallback, 1.0, beta)
    sigma = np.where(fallback, np.nan, sigma)
    if fallback.any():
        log.info("market_model_ar: %d из %d событий -- откат к market adjusted (мало наблюдений)", int(fallback.sum()), len(ev))

    raw = gather_event_window(ret, ev["secid"], ev["t0"], ws, we, ev["event_id"])
    mkt_w = gather_event_window(mkt, None, ev["t0"], ws, we, ev["event_id"])
    ar = raw - alpha[:, None] - beta[:, None] * mkt_w.to_numpy()
    params = pd.DataFrame(
        {
            "secid": ev["secid"].to_numpy(),
            "t0": ev["t0"].to_numpy(),
            "alpha": alpha,
            "beta": beta,
            "sigma": sigma,
            "n_obs": n_obs,
            "model": np.where(fallback, "market_adjusted", "market_model"),
        },
        index=pd.Index(ev["event_id"], name="event_id"),
    )
    return MarketModelResult(ar=ar, raw=raw, mkt=mkt_w, params=params)
