"""Event study: реакция котировок на раскрытия (CAR/BHAR, сводки, скрининг категорий).

Основные шаги (:func:`compute_car_table`):

1. Для каждого события определяется день t0 (:mod:`timing`), отбрасываются события без
   котировок, с короткой историей и по неликвидным бумагам (медианный дневной оборот за
   ``lookback_days`` до t0 меньше ``min_turnover_rub``).
2. Строится матрица аномальных доходностей «событие x относительный день» по выбранной
   модели (market adjusted или рыночная модель, см. :mod:`returns`).
3. Для каждого окна (start, end) в относительных торговых днях считаются
   CAR = сумма дневных AR, BHAR = prod(1+r) - prod(1+r_m) по дням с наблюдениями.
   Окно с долей наблюдений меньше ``min_window_coverage`` отбрасывается.

Сводка (:func:`summarize`) -- кросс-секционная статистика по группам: t-статистика среднего,
знаковый тест, bootstrap-интервал среднего. :func:`screen_categories` ранжирует категории
сообщений по |t| и применяет поправку Бенджамини--Хохберга (FDR).

:func:`calendar_time_portfolio` -- календарный портфель (Jaffe--Mandelker): устойчив к
кластеризации событий во времени; альфа оценивается OLS с ошибками Newey--West.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from disclosure_alpha.analysis.returns import (
    gather_event_window,
    index_returns,
    market_model_ar,
    simple_returns,
    to_wide,
    trading_calendar,
)
from disclosure_alpha.analysis.timing import as_datetime64, assign_event_days

log = logging.getLogger(__name__)

DEFAULT_WINDOWS: tuple[tuple[int, int], ...] = ((-20, -1), (-5, -1), (0, 0), (0, 1), (0, 5), (0, 10), (0, 20), (0, 60))
MODELS = ("market_adjusted", "market_model")
STAT_COLUMNS = (
    "n", "mean_car", "median_car", "std", "t_stat", "p_value", "share_positive", "sign_test_p",
    "ci_low", "ci_high", "mean_bhar", "rank", "q_value", "significant",
)


@dataclass
class EventStudyConfig:
    """Параметры event study (окна -- в относительных торговых днях от t0, включительно)."""

    windows: list[tuple[int, int]] = field(default_factory=lambda: list(DEFAULT_WINDOWS))
    model: str = "market_adjusted"
    cutoff_time: str = "18:40"
    min_price_obs: int = 20               # минимум наблюдений цены за lookback_days до t0
    lookback_days: int = 60               # окно для фильтров истории/ликвидности, дни (-lookback, -1)
    min_turnover_rub: float | None = 5e6  # медианный дневной оборот, руб.; None -- без фильтра
    est_window: tuple[int, int] = (-250, -30)
    min_est_obs: int = 100
    min_window_coverage: float = 0.8      # доля дней окна с наблюдениями, чтобы окно засчиталось
    bootstrap_iter: int = 2000
    random_state: int = 0

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise ValueError(f"model должен быть одним из {MODELS}, получено {self.model!r}")
        self.windows = [(int(s), int(e)) for s, e in self.windows]
        for s, e in self.windows:
            if s > e:
                raise ValueError(f"Окно {(s, e)}: start > end")


@dataclass
class EventStudyResult:
    """Таблица CAR по событиям и окнам + диагностика отбора событий."""

    table: pd.DataFrame
    diagnostics: dict

    def __iter__(self):
        yield self.table
        yield self.diagnostics


@dataclass
class PreparedEvents:
    """События после фильтров, с t0, и панель доходностей на общем календаре."""

    events: pd.DataFrame
    ret: pd.DataFrame
    mkt_ret: pd.Series
    calendar: pd.DatetimeIndex
    diagnostics: dict


@dataclass
class ArMatrices:
    """Матрицы «событие x относительный день»: AR, сырые доходности, доходность рынка."""

    ar: pd.DataFrame
    raw: pd.DataFrame
    mkt: pd.DataFrame
    model: pd.Series


@dataclass
class CalendarTimeResult:
    """Дневные доходности календарного портфеля и оценка альфы."""

    daily: pd.DataFrame
    stats: dict

    def __iter__(self):
        yield self.daily
        yield self.stats


def window_label(window: tuple[int, int] | str) -> str:
    """Строковая метка окна: (0, 20) -> "[0,20]"."""
    if isinstance(window, str):
        return window
    s, e = window
    return f"[{int(s)},{int(e)}]"


# ------------------------------------------------------------------------------- подготовка
def normalize_events(events: pd.DataFrame) -> pd.DataFrame:
    ev = events.copy()
    for col in ("event_id", "secid", "published_at"):
        if col not in ev.columns:
            raise KeyError(f"events: нет обязательной колонки {col!r}")
    if "category" not in ev.columns:
        log.warning("events: нет колонки category -- используется 'all'")
        ev["category"] = "all"
    if "direction" not in ev.columns:
        log.warning("events: нет колонки direction -- используется 0")
        ev["direction"] = 0
    ev["event_id"] = ev["event_id"].astype(str)
    ev["secid"] = ev["secid"].astype(str)
    ev["category"] = ev["category"].astype(str)
    ev["direction"] = pd.to_numeric(ev["direction"], errors="coerce").fillna(0).astype(int)
    ev["published_at"] = as_datetime64(ev["published_at"])
    return ev.reset_index(drop=True)


def prepare_events(events: pd.DataFrame, prices: pd.DataFrame, index: pd.DataFrame, config: EventStudyConfig) -> PreparedEvents:
    """Фильтрует события и строит панель доходностей (общий шаг для всех функций модуля)."""
    diag: dict = {"n_input": int(len(events))}
    ev = normalize_events(events)

    bad = ev["published_at"].isna() | ev["secid"].isin(["", "nan", "None"])
    diag["dropped_missing_fields"] = int(bad.sum())
    ev = ev[~bad]
    dup = ev["event_id"].duplicated(keep="first")
    diag["dropped_duplicate_id"] = int(dup.sum())
    if dup.any():
        log.warning("events: %d дубликатов event_id -- оставлен первый", int(dup.sum()))
    ev = ev[~dup]

    calendar = trading_calendar(prices, index)
    close = to_wide(prices, "close").reindex(calendar)
    ret = simple_returns(close)
    mkt_ret = index_returns(index, calendar)

    ev = ev.assign(t0=assign_event_days(ev, calendar, config.cutoff_time).to_numpy())
    no_t0 = ev["t0"].isna()
    diag["dropped_no_t0"] = int(no_t0.sum())
    ev = ev[~no_t0]
    unknown = ~ev["secid"].isin(close.columns)
    diag["dropped_unknown_secid"] = int(unknown.sum())
    if unknown.any():
        log.warning("events: %d событий по бумагам без котировок: %s", int(unknown.sum()), sorted(ev.loc[unknown, "secid"].unique())[:10])
    ev = ev[~unknown]

    lb = int(config.lookback_days)
    hist = gather_event_window(close, ev["secid"], ev["t0"], -lb, -1, ev["event_id"]).to_numpy()
    n_obs = np.isfinite(hist).sum(axis=1)
    short_hist = n_obs < int(config.min_price_obs)
    diag["dropped_insufficient_history"] = int(short_hist.sum())
    ev = ev[~short_hist]

    if config.min_turnover_rub is not None and "value" in prices.columns and len(ev):
        turnover = to_wide(prices, "value").reindex(calendar)
        tw = gather_event_window(turnover, ev["secid"], ev["t0"], -lb, -1, ev["event_id"]).to_numpy()
        with np.errstate(all="ignore"):
            med = np.nanmedian(np.where(np.isfinite(tw), tw, np.nan), axis=1)
        illiquid = ~(med >= float(config.min_turnover_rub))
        diag["dropped_illiquid"] = int(illiquid.sum())
        ev = ev.assign(median_turnover=med)[~illiquid]
    else:
        diag["dropped_illiquid"] = 0

    ev = ev.reset_index(drop=True)
    ev["t0_pos"] = calendar.get_indexer(pd.DatetimeIndex(ev["t0"]))
    diag["n_used"] = int(len(ev))
    log.info("prepare_events: %s", diag)
    return PreparedEvents(events=ev, ret=ret, mkt_ret=mkt_ret, calendar=calendar, diagnostics=diag)


def event_ar_matrix(prep: PreparedEvents, config: EventStudyConfig, start: int, end: int) -> ArMatrices:
    """Матрицы AR/raw/mkt по событиям ``prep.events`` на окне (start, end)."""
    ev = prep.events
    if config.model == "market_model":
        res = market_model_ar(
            prep.ret, prep.mkt_ret, ev[["event_id", "secid", "t0"]],
            est_window=config.est_window, min_obs=config.min_est_obs, event_window=(start, end),
        )
        model = res.params["model"].astype(str)
        model.index = ev.index
        return ArMatrices(ar=res.ar.set_axis(ev.index), raw=res.raw.set_axis(ev.index), mkt=res.mkt.set_axis(ev.index), model=model)
    raw = gather_event_window(prep.ret, ev["secid"], ev["t0"], start, end, ev.index)
    mkt = gather_event_window(prep.mkt_ret, None, ev["t0"], start, end, ev.index)
    ar = raw - mkt.to_numpy()
    model = pd.Series("market_adjusted", index=ev.index, dtype=str)
    return ArMatrices(ar=ar, raw=raw, mkt=mkt, model=model)


# ---------------------------------------------------------------------------------- CAR
def compute_car_table(events: pd.DataFrame, prices: pd.DataFrame, index: pd.DataFrame, config: EventStudyConfig | None = None) -> EventStudyResult:
    """Таблица «событие x окно»: CAR, BHAR, число наблюдений, сырая и рыночная доходность.

    Возвращает :class:`EventStudyResult` (распаковывается как ``table, diagnostics``).
    """
    config = config or EventStudyConfig()
    prep = prepare_events(events, prices, index, config)
    ev = prep.events
    extra_cols = [c for c in ("size",) if c in ev.columns]
    columns = ["event_id", "secid", "category", "direction", "t0", "window", "win_start", "win_end",
               "car", "bhar", "n_days", "raw_ret", "mkt_ret", "model", *extra_cols]
    diag = dict(prep.diagnostics)
    diag["incomplete_window"] = {}
    if not len(ev) or not config.windows:
        return EventStudyResult(pd.DataFrame(columns=columns), diag)

    span_start = min(s for s, _ in config.windows)
    span_end = max(e for _, e in config.windows)
    mats = event_ar_matrix(prep, config, span_start, span_end)
    parts = []
    for s, e in config.windows:
        cols = list(range(s, e + 1))
        a = mats.ar[cols].to_numpy()
        r = mats.raw[cols].to_numpy()
        m = mats.mkt[cols].to_numpy()
        valid = np.isfinite(a) & np.isfinite(r) & np.isfinite(m)
        n_days = valid.sum(axis=1)
        car = np.where(valid, a, 0.0).sum(axis=1)
        raw_ret = np.prod(np.where(valid, 1.0 + r, 1.0), axis=1) - 1.0
        mkt_ret = np.prod(np.where(valid, 1.0 + m, 1.0), axis=1) - 1.0
        need = max(1, math.ceil(config.min_window_coverage * len(cols)))
        keep = n_days >= need
        diag["incomplete_window"][window_label((s, e))] = int((~keep).sum())
        part = pd.DataFrame({
            "event_id": ev["event_id"].to_numpy(),
            "secid": ev["secid"].to_numpy(),
            "category": ev["category"].to_numpy(),
            "direction": ev["direction"].to_numpy(),
            "t0": ev["t0"].to_numpy(),
            "window": window_label((s, e)),
            "win_start": s,
            "win_end": e,
            "car": car,
            "bhar": raw_ret - mkt_ret,
            "n_days": n_days,
            "raw_ret": raw_ret,
            "mkt_ret": mkt_ret,
            "model": mats.model.to_numpy(),
            **{c: ev[c].to_numpy() for c in extra_cols},
        })
        parts.append(part[keep])
    table = pd.concat(parts, ignore_index=True)[columns]
    diag["n_rows"] = int(len(table))
    return EventStudyResult(table, diag)


def signed_car(car_table: pd.DataFrame, drop_neutral: bool = True) -> pd.DataFrame:
    """Добавляет signed_car/signed_bhar/signed_raw_ret = значение x direction.

    Позволяет объединять покупки и продажи инсайдеров: положительный signed CAR означает,
    что рынок движется в сторону сделки инсайдера. События с direction=0 отбрасываются
    (``drop_neutral=True``) либо получают NaN.
    """
    out = car_table.copy()
    d = out["direction"].astype(float)
    d = d.where(d != 0, np.nan)
    for col in ("car", "bhar", "raw_ret"):
        if col in out.columns:
            out[f"signed_{col}"] = out[col] * d
    if drop_neutral:
        out = out[out["direction"] != 0].reset_index(drop=True)
    return out


# --------------------------------------------------------------------------------- сводка
def _bootstrap_ci(x: np.ndarray, n_iter: int, rng: np.random.Generator, chunk: int = 256) -> tuple[float, float]:
    n = len(x)
    if n < 2 or n_iter <= 0:
        return (np.nan, np.nan)
    means = np.empty(n_iter)
    done = 0
    while done < n_iter:
        b = min(chunk, n_iter - done)
        idx = rng.integers(0, n, size=(b, n))
        means[done:done + b] = x[idx].mean(axis=1)
        done += b
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(lo), float(hi))


def _group_stats(x: np.ndarray, bhar: np.ndarray | None, n_iter: int, rng: np.random.Generator) -> dict:
    x = x[np.isfinite(x)]
    n = len(x)
    out = {"n": n, "mean_car": np.nan, "median_car": np.nan, "std": np.nan, "t_stat": np.nan, "p_value": np.nan,
           "share_positive": np.nan, "sign_test_p": np.nan, "ci_low": np.nan, "ci_high": np.nan, "mean_bhar": np.nan}
    if n == 0:
        return out
    out["mean_car"] = float(x.mean())
    out["median_car"] = float(np.median(x))
    out["share_positive"] = float((x > 0).mean())
    if n >= 2:
        sd = float(x.std(ddof=1))
        out["std"] = sd
        if sd > 0:
            t = out["mean_car"] / (sd / math.sqrt(n))
            out["t_stat"] = float(t)
            out["p_value"] = float(2.0 * stats.t.sf(abs(t), n - 1))
        n_nonzero = int((x != 0).sum())
        if n_nonzero > 0:
            out["sign_test_p"] = float(stats.binomtest(int((x > 0).sum()), n_nonzero, 0.5).pvalue)
        out["ci_low"], out["ci_high"] = _bootstrap_ci(x, n_iter, rng)
    if bhar is not None:
        b = bhar[np.isfinite(bhar)]
        if len(b):
            out["mean_bhar"] = float(b.mean())
    return out


def summarize(
    car_table: pd.DataFrame,
    by: Sequence[str] = ("category", "direction", "window"),
    value_col: str = "car",
    bootstrap_iter: int = 2000,
    random_state: int = 0,
) -> pd.DataFrame:
    """Кросс-секционная сводка по группам ``by``.

    Колонки: n, mean_car, median_car, std, t_stat (среднее / стандартная ошибка), p_value
    (двусторонний t-тест), share_positive, sign_test_p (биномиальный тест знаков, нули
    исключены), ci_low/ci_high (bootstrap 2.5/97.5% среднего), mean_bhar (для value_col
    «car» -- по колонке bhar, для «signed_car» -- signed_bhar).
    """
    by = list(by)
    if value_col not in car_table.columns:
        raise KeyError(f"нет колонки {value_col!r}")
    bhar_col = {"car": "bhar", "signed_car": "signed_bhar"}.get(value_col)
    if bhar_col is not None and bhar_col not in car_table.columns:
        bhar_col = None
    rng = np.random.default_rng(random_state)
    rows = []
    if not len(car_table):
        return pd.DataFrame(columns=[*by, *STAT_COLUMNS[:11]])
    grouped = car_table.groupby(by, sort=True, observed=True, dropna=False)
    for key, g in grouped:
        key = key if isinstance(key, tuple) else (key,)
        st = _group_stats(g[value_col].to_numpy(dtype=float), g[bhar_col].to_numpy(dtype=float) if bhar_col else None, bootstrap_iter, rng)
        rows.append({**dict(zip(by, key)), **st})
    out = pd.DataFrame(rows)
    if "win_start" in car_table.columns and "window" in by:
        order = car_table.drop_duplicates("window").set_index("window")[["win_start", "win_end"]]
        out = out.assign(_s=out["window"].map(order["win_start"]), _e=out["window"].map(order["win_end"]))
        sort_cols = [c for c in by if c != "window"] + ["_s", "_e"]
        out = out.sort_values(sort_cols).drop(columns=["_s", "_e"]).reset_index(drop=True)
    return out


def benjamini_hochberg(p_values: np.ndarray) -> np.ndarray:
    """q-значения (FDR) по Бенджамини--Хохбергу; NaN остаются NaN."""
    p = np.asarray(p_values, dtype=float)
    q = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    m = int(ok.sum())
    if m == 0:
        return q
    pv = p[ok]
    order = np.argsort(pv)
    ranked = pv[order] * m / np.arange(1, m + 1)
    adj = np.minimum.accumulate(ranked[::-1])[::-1]
    qq = np.empty(m)
    qq[order] = np.clip(adj, 0.0, 1.0)
    q[ok] = qq
    return q


def screen_categories(summary: pd.DataFrame, window: tuple[int, int] | str = (0, 20), min_n: int = 30, alpha: float = 0.05) -> pd.DataFrame:
    """Скрининг «какие типы сообщений несут информацию».

    Берёт строки сводки (:func:`summarize`) для окна ``window`` с n >= min_n, ранжирует по
    |t_stat| и применяет FDR-поправку Бенджамини--Хохберга по всем группам (категория или
    категория x направление -- зависит от ``by`` сводки). Колонки: ключи группы, n, mean_car,
    t_stat, p_value, q_value, rank, significant (q <= alpha).
    """
    df = summary.copy()
    if "window" in df.columns:
        df = df[df["window"] == window_label(window)]
    df = df[(df["n"] >= min_n) & df["p_value"].notna()].copy()
    key_cols = [c for c in df.columns if c not in STAT_COLUMNS and c != "window"]
    if not len(df):
        return pd.DataFrame(columns=[*key_cols, "n", "mean_car", "mean_bhar", "t_stat", "p_value", "q_value", "rank", "significant"])
    df["q_value"] = benjamini_hochberg(df["p_value"].to_numpy())
    df["abs_t"] = df["t_stat"].abs()
    df = df.sort_values("abs_t", ascending=False).drop(columns="abs_t").reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)
    df["significant"] = df["q_value"] <= alpha
    cols = [*key_cols, "n", "mean_car", "mean_bhar", "t_stat", "p_value", "q_value", "rank", "significant"]
    return df[[c for c in cols if c in df.columns]]


# ------------------------------------------------------------------------------ CAR-кривая
def average_ar_path(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    index: pd.DataFrame,
    config: EventStudyConfig | None = None,
    start: int = -20,
    end: int = 60,
    signed: bool = False,
) -> pd.DataFrame:
    """Средний AR и накопленный CAR по относительным дням (классическая кривая CAR).

    Формат long: category, direction, rel_day, mean_ar, mean_car (сумма mean_ar от ``start``), n.
    При ``signed=True`` AR умножается на direction, события с direction=0 отбрасываются,
    группировка только по категории (direction = "signed").
    """
    config = config or EventStudyConfig()
    prep = prepare_events(events, prices, index, config)
    ev = prep.events
    if not len(ev):
        return pd.DataFrame(columns=["category", "direction", "rel_day", "mean_ar", "mean_car", "n"])
    mats = event_ar_matrix(prep, config, start, end)
    ar = mats.ar.to_numpy()
    rel_days = np.arange(start, end + 1)
    if signed:
        keep = ev["direction"].to_numpy() != 0
        ar = ar[keep] * ev["direction"].to_numpy()[keep][:, None]
        keys = pd.DataFrame({"category": ev["category"].to_numpy()[keep], "direction": "signed"})
    else:
        keys = ev[["category", "direction"]].reset_index(drop=True)
    parts = []
    for (cat, d), g in keys.groupby(["category", "direction"], sort=True):
        block = ar[g.index.to_numpy()]
        n = np.isfinite(block).sum(axis=0)
        with np.errstate(invalid="ignore"):
            mean_ar = np.where(n > 0, np.nansum(block, axis=0) / np.maximum(n, 1), np.nan)
        mean_car = np.cumsum(np.nan_to_num(mean_ar))
        parts.append(pd.DataFrame({"category": cat, "direction": d, "rel_day": rel_days, "mean_ar": mean_ar, "mean_car": mean_car, "n": n}))
    return pd.concat(parts, ignore_index=True)


# ------------------------------------------------------------------- календарный портфель
def _direction_set(direction_filter) -> set[int] | None:
    if direction_filter is None:
        return None
    if isinstance(direction_filter, (int, np.integer)):
        return {int(direction_filter)}
    return {int(d) for d in direction_filter}


def calendar_time_portfolio(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    index: pd.DataFrame,
    hold_days: int = 20,
    direction_filter: int | Iterable[int] | None = +1,
    config: EventStudyConfig | None = None,
    categories: Iterable[str] | None = None,
    entry_lag: int = 1,
    hac_lags: int = 5,
) -> CalendarTimeResult:
    """Календарный портфель (Jaffe--Mandelker): равновзвешенный портфель всех бумаг,
    у которых событие было в последние ``hold_days`` торговых дней.

    Бумага входит в портфель на дни t0+entry_lag ... t0+entry_lag+hold_days-1 (по умолчанию
    с дня после t0 -- реализуемо на практике). Доходность портфеля в день -- среднее дневных
    доходностей участников (бумага с несколькими событиями учитывается один раз). Дни без
    позиций -> NaN и не участвуют в регрессии. Альфа: OLS r_p = alpha + beta r_m + e с
    HAC (Newey--West, ``hac_lags``) ошибками, rf = 0.

    Возвращает :class:`CalendarTimeResult`: daily (date, port_ret, mkt_ret, n_stocks) и
    stats (alpha, alpha_annual, alpha_t, alpha_p, beta, beta_t, n_days, r2, n_events).
    """
    config = config or EventStudyConfig()
    prep = prepare_events(events, prices, index, config)
    ev = prep.events
    dirs = _direction_set(direction_filter)
    if dirs is not None:
        ev = ev[ev["direction"].isin(dirs)]
    if categories is not None:
        ev = ev[ev["category"].isin(set(categories))]
    cal = prep.calendar
    ret_arr = prep.ret.to_numpy()
    n_days, n_sec = ret_arr.shape
    col_map = {s: i for i, s in enumerate(prep.ret.columns)}
    cols = np.array([col_map[s] for s in ev["secid"]], dtype=int)
    start = ev["t0_pos"].to_numpy() + int(entry_lag)
    end = start + int(hold_days)  # не включительно
    ok = start < n_days
    diff = np.zeros((n_days + 1, max(n_sec, 1)))
    np.add.at(diff, (np.clip(start[ok], 0, n_days), cols[ok]), 1.0)
    np.add.at(diff, (np.clip(end[ok], 0, n_days), cols[ok]), -1.0)
    member = np.cumsum(diff, axis=0)[:n_days, :n_sec] > 0
    w = member & np.isfinite(ret_arr)
    n_stocks = w.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        port = np.where(n_stocks > 0, np.where(w, ret_arr, 0.0).sum(axis=1) / np.maximum(n_stocks, 1), np.nan)
    daily = pd.DataFrame({"date": cal, "port_ret": port, "mkt_ret": prep.mkt_ret.reindex(cal).to_numpy(), "n_stocks": n_stocks})

    st: dict = {"alpha": np.nan, "alpha_annual": np.nan, "alpha_t": np.nan, "alpha_p": np.nan, "beta": np.nan, "beta_t": np.nan,
                "n_days": 0, "r2": np.nan, "n_events": int(ok.sum()), "mean_port_ret": np.nan, "hac_lags": int(hac_lags)}
    reg = daily.dropna(subset=["port_ret", "mkt_ret"])
    reg = reg[reg["n_stocks"] > 0]
    st["n_days"] = int(len(reg))
    if len(reg) > max(10, hac_lags + 3) and reg["mkt_ret"].std() > 0:
        import statsmodels.api as sm

        x = sm.add_constant(reg["mkt_ret"].to_numpy())
        fit = sm.OLS(reg["port_ret"].to_numpy(), x).fit(cov_type="HAC", cov_kwds={"maxlags": int(hac_lags)})
        st.update({
            "alpha": float(fit.params[0]), "alpha_annual": float(fit.params[0] * 252), "alpha_t": float(fit.tvalues[0]),
            "alpha_p": float(fit.pvalues[0]), "beta": float(fit.params[1]), "beta_t": float(fit.tvalues[1]),
            "r2": float(fit.rsquared), "mean_port_ret": float(reg["port_ret"].mean()),
        })
    return CalendarTimeResult(daily=daily, stats=st)
