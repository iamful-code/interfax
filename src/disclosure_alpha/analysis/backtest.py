"""Бэктест событийной стратегии: сделка на каждое раскрытие, фиксированный срок удержания.

Механика (по торговым дням календаря котировок):

* Сигнал -> дата входа (:mod:`timing`): ``next_open`` -- открытие первой сессии после
  публикации, ``t0_close`` -- закрытие дня t0, ``next_close`` -- закрытие дня t0+1. Если в
  день входа цены нет (остановка торгов), вход переносится не более чем на
  ``max_entry_delay`` дней, иначе сигнал пропускается.
* Размер позиции: ``position_weight="equal"`` -- капитал (текущий equity по закрытию
  предыдущего дня) / ``max_positions``; ``"fixed"`` -- начальный капитал / ``max_positions``.
  Сверху ограничение ликвидностью: не более ``max_participation`` x медианный дневной
  оборот за ``liquidity_lookback`` дней до входа; бумаги с оборотом ниже ``min_turnover_rub``
  пропускаются. Количество -- целое число акций (лоты не учитываются). Если свободных
  денег меньше ``min_cash_fraction`` x целевого объёма или нет свободного слота --
  сигнал пропускается (учитывается в ``metrics["skipped_signals"]``).
* Исполнение: цена входа = open/close x (1 + side x slippage), цена выхода = close x
  (1 - side x slippage); комиссия ``commission_bps`` от объёма на входе и на выходе.
  gross_ret = side x (exit_raw / entry_raw - 1) -- до издержек; net_ret = pnl / объём входа.
* Выход: по закрытию ``hold_days``-го торгового дня после дня входа (``hold_days``);
  по стоп-лоссу (проверка по закрытию, ``stop_loss``); при делистинге -- по последней
  доступной цене закрытия (``delisted``); в последний день бэктеста (``end``).
  В дни без котировки (остановка торгов) позиция оценивается по последней цене.
* Шорты (``allow_short=True``): продажа по open, откуп по close; маржа не моделируется,
  требование к свободным деньгам такое же, как для длинной позиции (как обеспечение).

Метрики считаются по дневному ряду equity (252 дня в году, rf = 0).
"""
from __future__ import annotations

import itertools
import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from disclosure_alpha.analysis.event_study import normalize_events
from disclosure_alpha.analysis.returns import index_series, to_wide, trading_calendar
from disclosure_alpha.analysis.timing import assign_event_days, entry_days_next_open

log = logging.getLogger(__name__)

ENTRY_MODES = ("next_open", "next_close", "t0_close")
TRADE_COLUMNS = [
    "trade_id", "event_id", "secid", "category", "direction", "side", "entry_date", "entry_price", "exit_date",
    "exit_price", "qty", "gross_ret", "net_ret", "pnl", "hold_days", "exit_reason", "entry_price_raw",
    "exit_price_raw", "notional", "commission",
]
DAILY_COLUMNS = ["date", "equity", "cash", "exposure", "n_positions", "ret", "index_ret", "index_equity"]
TRADING_DAYS_PER_YEAR = 252


@dataclass
class BacktestConfig:
    """Параметры бэктеста (см. докстринг модуля)."""

    hold_days: int = 20
    entry: str = "next_open"
    max_positions: int = 20
    position_weight: str = "equal"          # equal | fixed
    commission_bps: float = 5.0
    slippage_bps: float = 10.0
    allow_short: bool = False
    direction_filter: tuple[int, ...] | int | None = (1,)
    stop_loss: float | None = None          # доля от цены входа, напр. 0.1
    initial_capital: float = 1e6
    min_turnover_rub: float | None = 5e6
    liquidity_lookback: int = 60
    max_participation: float | None = 0.02
    start: str | pd.Timestamp | None = None  # ограничение на дату входа (включительно)
    end: str | pd.Timestamp | None = None
    categories: tuple[str, ...] | None = None
    cutoff_time: str = "18:40"
    open_time: str = "10:00"
    allow_multiple_per_secid: bool = False   # False -- сигнал по уже открытой бумаге пропускается
    max_entry_delay: int = 3
    min_cash_fraction: float = 0.5

    def __post_init__(self) -> None:
        if self.entry not in ENTRY_MODES:
            raise ValueError(f"entry должен быть одним из {ENTRY_MODES}")
        if self.position_weight not in ("equal", "fixed"):
            raise ValueError("position_weight: 'equal' или 'fixed'")
        if self.hold_days < 1 or self.max_positions < 1:
            raise ValueError("hold_days и max_positions должны быть >= 1")
        if self.categories is not None:
            self.categories = tuple(self.categories)


@dataclass
class Panel:
    """Подготовленные массивы котировок (день x бумага) для быстрого прогона."""

    calendar: pd.DatetimeIndex
    secids: list[str]
    open: np.ndarray
    close: np.ndarray
    last_close: np.ndarray       # close с forward-fill (оценка позиции в дни без торгов)
    last_valid_pos: np.ndarray   # последний день с ценой по бумаге (-1 если нет)
    adv: np.ndarray              # медианный оборот за lookback дней ДО текущего дня
    mkt_close: np.ndarray        # бенчмарк (forward-fill)

    @property
    def col_index(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.secids)}


@dataclass
class BacktestResult:
    """Сделки, дневной ряд, метрики, конфигурация и таблица сигналов со статусами."""

    trades: pd.DataFrame
    daily: pd.DataFrame
    metrics: dict
    config: BacktestConfig
    signals: pd.DataFrame = field(default_factory=pd.DataFrame)


# ------------------------------------------------------------------------------ подготовка
def prepare_panel(prices: pd.DataFrame, index: pd.DataFrame, config: BacktestConfig | None = None) -> Panel:
    """Строит :class:`Panel` (один раз на набор котировок; переиспользуется в placebo/walk-forward)."""
    config = config or BacktestConfig()
    cal = trading_calendar(prices, index)
    close = to_wide(prices, "close").reindex(cal)
    open_ = to_wide(prices, "open").reindex(cal).reindex(columns=close.columns) if "open" in prices.columns else close.copy()
    if "value" in prices.columns:
        value = to_wide(prices, "value").reindex(cal).reindex(columns=close.columns)
        lb = int(config.liquidity_lookback)
        adv = value.rolling(lb, min_periods=max(5, lb // 3)).median().shift(1)
    else:
        adv = pd.DataFrame(np.nan, index=cal, columns=close.columns)
    close_arr = close.to_numpy(dtype=float)
    valid = np.isfinite(close_arr)
    last_valid = np.where(valid.any(axis=0), close_arr.shape[0] - 1 - np.argmax(valid[::-1], axis=0), -1)
    mkt = index_series(index, cal).to_numpy(dtype=float)
    return Panel(
        calendar=cal, secids=list(close.columns), open=open_.to_numpy(dtype=float), close=close_arr,
        last_close=close.ffill().to_numpy(dtype=float), last_valid_pos=last_valid, adv=adv.to_numpy(dtype=float), mkt_close=mkt,
    )


def _direction_set(direction_filter) -> set[int] | None:
    if direction_filter is None:
        return None
    if isinstance(direction_filter, (int, np.integer)):
        return {int(direction_filter)}
    return {int(d) for d in direction_filter}


def _to_pos(cal: pd.DatetimeIndex, value, side: str) -> int | None:
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if side == "start":
        return int(cal.searchsorted(ts, side="left"))
    return int(cal.searchsorted(ts, side="right") - 1)


def prepare_signals(events: pd.DataFrame, panel: Panel, config: BacktestConfig) -> tuple[pd.DataFrame, int, int]:
    """Сигналы с датой/позицией входа и статусом; возвращает (signals, start_pos, end_pos)."""
    cal = panel.calendar
    n = len(cal)
    ev = normalize_events(events)
    ev = ev[ev["published_at"].notna()]
    if config.categories is not None:
        ev = ev[ev["category"].isin(set(config.categories))]
    dirs = _direction_set(config.direction_filter)
    if dirs is not None:
        ev = ev[ev["direction"].isin(dirs)]
    ev = ev[ev["direction"] != 0].copy()
    ev["status"] = "pending"
    ev["skip_reason"] = ""
    ev["side"] = ev["direction"].astype(int)
    if not config.allow_short:
        short = ev["side"] < 0
        ev.loc[short, ["status", "skip_reason"]] = ["skipped", "short_not_allowed"]

    t0 = assign_event_days(ev, cal, config.cutoff_time)
    t0_pos = cal.get_indexer(pd.DatetimeIndex(t0))
    if config.entry == "next_open":
        entry_pos = cal.get_indexer(pd.DatetimeIndex(entry_days_next_open(ev, cal, config.open_time)))
    elif config.entry == "t0_close":
        entry_pos = t0_pos
    else:  # next_close
        entry_pos = np.where(t0_pos >= 0, t0_pos + 1, -1)
    entry_pos = np.where(entry_pos >= n, -1, entry_pos)
    ev["t0"] = t0.to_numpy()
    ev["entry_pos"] = entry_pos
    ev["entry_date"] = pd.Series(np.where(entry_pos >= 0, cal.to_numpy()[np.clip(entry_pos, 0, max(n - 1, 0))], np.datetime64("NaT")), index=ev.index)
    col_map = panel.col_index
    ev["col"] = [col_map.get(s, -1) for s in ev["secid"]]

    pending = ev["status"] == "pending"
    no_day = pending & (ev["entry_pos"] < 0)
    ev.loc[no_day, ["status", "skip_reason"]] = ["skipped", "no_trading_day"]
    unknown = (ev["status"] == "pending") & (ev["col"] < 0)
    ev.loc[unknown, ["status", "skip_reason"]] = ["skipped", "unknown_secid"]

    start_pos = _to_pos(cal, config.start, "start")
    end_pos = _to_pos(cal, config.end, "end")
    live = ev["status"] == "pending"
    if start_pos is None:
        start_pos = int(ev.loc[live, "entry_pos"].min()) if live.any() else 0
    if end_pos is None:
        end_pos = n - 1
    start_pos = max(0, min(start_pos, n - 1))
    end_pos = max(0, min(end_pos, n - 1))
    outside = live & ((ev["entry_pos"] < start_pos) | (ev["entry_pos"] > end_pos))
    ev.loc[outside, ["status", "skip_reason"]] = ["skipped", "outside_range"]
    ev = ev.sort_values(["entry_pos", "published_at", "event_id"], kind="stable").reset_index(drop=True)
    return ev, start_pos, end_pos


# ----------------------------------------------------------------------------------- прогон
def run_backtest(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    index: pd.DataFrame,
    config: BacktestConfig | None = None,
    panel: Panel | None = None,
) -> BacktestResult:
    """Прогоняет стратегию «сделка на событие» и возвращает :class:`BacktestResult`."""
    config = config or BacktestConfig()
    panel = panel or prepare_panel(prices, index, config)
    signals, start_pos, end_pos = prepare_signals(events, panel, config)
    cal = panel.calendar
    n_days = len(cal)
    if n_days == 0:
        raise ValueError("Пустой календарь котировок")

    comm = config.commission_bps / 1e4
    slip = config.slippage_bps / 1e4
    cash = float(config.initial_capital)
    equity_prev = cash
    positions: dict[int, dict] = {}
    held_secids: dict[str, int] = defaultdict(int)
    trades: list[dict] = []
    status = signals["status"].to_numpy().astype(object)
    reason = signals["skip_reason"].to_numpy().astype(object)
    sig_entry_pos = signals["entry_pos"].to_numpy()
    sig_col = signals["col"].to_numpy()
    sig_side = signals["side"].to_numpy()
    sig_secid = signals["secid"].to_numpy()
    sig_event = signals["event_id"].to_numpy()
    sig_cat = signals["category"].to_numpy()
    sig_dir = signals["direction"].to_numpy()

    by_day: dict[int, list[tuple[int, int]]] = defaultdict(list)  # день -> [(idx сигнала, число переносов)]
    for k in np.flatnonzero(status == "pending"):
        by_day[int(sig_entry_pos[k])].append((int(k), 0))
    next_trade_id = 1
    close_entry = config.entry != "next_open"
    price_src = panel.close if close_entry else panel.open

    daily_rows = []

    def close_position(pos: dict, day: int, px_raw: float, why: str) -> None:
        nonlocal cash
        side = pos["side"]
        fill = px_raw * (1.0 - side * slip)
        commission = pos["qty"] * fill * comm
        cash += side * pos["qty"] * fill - commission
        pnl = side * pos["qty"] * (fill - pos["entry_fill"]) - pos["entry_commission"] - commission
        notional = pos["qty"] * pos["entry_fill"]
        trades.append({
            "trade_id": pos["trade_id"], "event_id": pos["event_id"], "secid": pos["secid"], "category": pos["category"],
            "direction": pos["direction"], "side": side, "entry_date": cal[pos["entry_pos"]], "entry_price": pos["entry_fill"],
            "exit_date": cal[day], "exit_price": fill, "qty": pos["qty"],
            "gross_ret": side * (px_raw / pos["entry_price_raw"] - 1.0), "net_ret": pnl / notional, "pnl": pnl,
            "hold_days": int(day - pos["entry_pos"]), "exit_reason": why, "entry_price_raw": pos["entry_price_raw"],
            "exit_price_raw": px_raw, "notional": notional, "commission": pos["entry_commission"] + commission,
        })
        held_secids[pos["secid"]] -= 1
        del positions[pos["trade_id"]]

    def open_positions_from_signals(day: int) -> None:
        nonlocal cash, next_trade_id
        for k, delay in by_day.pop(day, []):
            col = sig_col[k]
            px_raw = price_src[day, col]
            if not np.isfinite(px_raw):
                if delay < config.max_entry_delay and day + 1 <= end_pos:
                    by_day[day + 1].append((k, delay + 1))
                else:
                    status[k], reason[k] = "skipped", "no_price"
                continue
            side = int(sig_side[k])
            secid = sig_secid[k]
            if not config.allow_multiple_per_secid and held_secids[secid] > 0:
                status[k], reason[k] = "skipped", "already_held"
                continue
            if len(positions) >= config.max_positions:
                status[k], reason[k] = "skipped", "no_slot"
                continue
            adv = panel.adv[day, col]
            if config.min_turnover_rub is not None and not (adv >= config.min_turnover_rub):
                status[k], reason[k] = "skipped", "illiquid"
                continue
            ref = equity_prev if config.position_weight == "equal" else config.initial_capital
            target = ref / config.max_positions
            if config.max_participation is not None and np.isfinite(adv):
                target = min(target, config.max_participation * adv)
            if cash < config.min_cash_fraction * target or target <= 0:
                status[k], reason[k] = "skipped", "no_cash"
                continue
            notional = min(target, cash)
            fill = px_raw * (1.0 + side * slip)
            qty = math.floor(notional / fill)
            if qty < 1:
                status[k], reason[k] = "skipped", "qty_zero"
                continue
            commission = qty * fill * comm
            cash -= side * qty * fill + commission
            positions[next_trade_id] = {
                "trade_id": next_trade_id, "event_id": sig_event[k], "secid": secid, "col": col, "side": side, "qty": qty,
                "entry_pos": day, "entry_price_raw": px_raw, "entry_fill": fill, "entry_commission": commission,
                "exit_pos": day + config.hold_days, "category": sig_cat[k], "direction": int(sig_dir[k]),
            }
            held_secids[secid] += 1
            status[k], reason[k] = "executed", ""
            next_trade_id += 1

    def process_exits(day: int) -> None:
        for pos in list(positions.values()):
            col = pos["col"]
            px = panel.close[day, col]
            if not np.isfinite(px):
                if day > panel.last_valid_pos[col]:
                    lv = int(panel.last_valid_pos[col])
                    if lv >= pos["entry_pos"]:
                        close_position(pos, lv, panel.close[lv, col], "delisted")
                    else:  # цены нет вовсе -- закрываем по цене входа
                        close_position(pos, day, pos["entry_price_raw"], "delisted")
                elif day == end_pos:
                    close_position(pos, day, panel.last_close[day, col], "end")
                continue
            if config.stop_loss is not None and pos["side"] * (px / pos["entry_fill"] - 1.0) <= -config.stop_loss:
                close_position(pos, day, px, "stop_loss")
            elif day >= pos["exit_pos"]:
                close_position(pos, day, px, "hold_days")
            elif day == end_pos:
                close_position(pos, day, px, "end")

    mkt = panel.mkt_close
    base_pos = start_pos - 1 if start_pos > 0 else start_pos
    mkt_base = mkt[base_pos] if np.isfinite(mkt[base_pos]) else np.nan
    mkt_prev = mkt_base
    for day in range(start_pos, end_pos + 1):
        if close_entry:
            process_exits(day)
            open_positions_from_signals(day)
        else:
            open_positions_from_signals(day)
            process_exits(day)
        pos_value = 0.0
        gross = 0.0
        for pos in positions.values():
            v = pos["qty"] * panel.last_close[day, pos["col"]]
            if not np.isfinite(v):
                v = pos["qty"] * pos["entry_price_raw"]
            pos_value += pos["side"] * v
            gross += abs(v)
        equity = cash + pos_value
        m = mkt[day]
        idx_ret = (m / mkt_prev - 1.0) if np.isfinite(m) and np.isfinite(mkt_prev) and mkt_prev != 0 else np.nan
        daily_rows.append({
            "date": cal[day], "equity": equity, "cash": cash, "exposure": gross / equity if equity != 0 else np.nan,
            "n_positions": len(positions), "ret": equity / equity_prev - 1.0 if equity_prev != 0 else np.nan,
            "index_ret": idx_ret, "index_equity": config.initial_capital * m / mkt_base if np.isfinite(m) and np.isfinite(mkt_base) else np.nan,
        })
        equity_prev = equity
        if np.isfinite(m):
            mkt_prev = m

    # сигналы, оставшиеся в очереди (перенос за пределы диапазона)
    for day_list in by_day.values():
        for k, _ in day_list:
            if status[k] == "pending":
                status[k], reason[k] = "skipped", "no_price"
    signals = signals.assign(status=status, skip_reason=reason)
    trades_df = pd.DataFrame(trades, columns=TRADE_COLUMNS) if trades else pd.DataFrame(columns=TRADE_COLUMNS)
    daily_df = pd.DataFrame(daily_rows, columns=DAILY_COLUMNS)
    metrics = compute_metrics(daily_df, trades_df, config, signals)
    log.info("backtest: %d сделок, total_return=%.4f, sharpe=%s", len(trades_df), metrics["total_return"], metrics["sharpe"])
    return BacktestResult(trades=trades_df, daily=daily_df, metrics=metrics, config=config, signals=signals)


# ---------------------------------------------------------------------------------- метрики
def _nan(x) -> float:
    return float(x) if x is not None and np.isfinite(x) else float("nan")


def compute_metrics(daily: pd.DataFrame, trades: pd.DataFrame, config: BacktestConfig, signals: pd.DataFrame | None = None) -> dict:
    """Метрики по дневному ряду equity и списку сделок."""
    m: dict = {}
    initial = float(config.initial_capital)
    eq = daily["equity"].to_numpy(dtype=float) if len(daily) else np.array([initial])
    r = daily["ret"].to_numpy(dtype=float) if len(daily) else np.array([])
    r = r[np.isfinite(r)]
    n = len(daily)
    years = n / TRADING_DAYS_PER_YEAR if n else np.nan
    m["n_days"] = int(n)
    m["total_return"] = float(eq[-1] / initial - 1.0)
    m["cagr"] = _nan((eq[-1] / initial) ** (1.0 / years) - 1.0) if n and eq[-1] > 0 and years > 0 else float("nan")
    sd = float(r.std(ddof=1)) if len(r) > 1 else float("nan")
    mu = float(r.mean()) if len(r) else float("nan")
    m["annual_vol"] = _nan(sd * math.sqrt(TRADING_DAYS_PER_YEAR))
    m["sharpe"] = _nan(mu / sd * math.sqrt(TRADING_DAYS_PER_YEAR)) if sd and sd > 0 else float("nan")
    downside = float(np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))) if len(r) else float("nan")
    m["sortino"] = _nan(mu / downside * math.sqrt(TRADING_DAYS_PER_YEAR)) if downside and downside > 0 else float("nan")
    dd = eq / np.maximum.accumulate(eq) - 1.0
    m["max_drawdown"] = float(dd.min())
    m["calmar"] = _nan(m["cagr"] / abs(m["max_drawdown"])) if m["max_drawdown"] < 0 and np.isfinite(m["cagr"]) else float("nan")

    m["n_trades"] = int(len(trades))
    if len(trades):
        net = trades["net_ret"].to_numpy(dtype=float)
        pnl = trades["pnl"].to_numpy(dtype=float)
        m["hit_rate"] = float((net > 0).mean())
        m["avg_win"] = _nan(net[net > 0].mean()) if (net > 0).any() else float("nan")
        m["avg_loss"] = _nan(net[net <= 0].mean()) if (net <= 0).any() else float("nan")
        losses = -pnl[pnl < 0].sum()
        m["profit_factor"] = _nan(pnl[pnl > 0].sum() / losses) if losses > 0 else float("inf") if (pnl > 0).any() else float("nan")
        m["avg_hold_days"] = float(trades["hold_days"].mean())
        m["avg_net_ret"] = float(net.mean())
        traded = float((trades["qty"] * (trades["entry_price"] + trades["exit_price"])).sum())
        m["turnover"] = _nan(traded / eq.mean() / years) if years and years > 0 else float("nan")
        m["total_commission"] = float(trades["commission"].sum())
        m["exit_reasons"] = trades["exit_reason"].value_counts().to_dict()
    else:
        m.update({"hit_rate": float("nan"), "avg_win": float("nan"), "avg_loss": float("nan"), "profit_factor": float("nan"),
                  "avg_hold_days": float("nan"), "avg_net_ret": float("nan"), "turnover": 0.0, "total_commission": 0.0, "exit_reasons": {}})

    m.update({"alpha_daily": float("nan"), "alpha_annual": float("nan"), "alpha_t": float("nan"), "beta": float("nan"),
              "information_ratio": float("nan"), "index_total_return": float("nan")})
    if len(daily):
        both = daily[["ret", "index_ret"]].dropna()
        if len(both) > 10 and both["index_ret"].std() > 0 and both["ret"].std() > 0:
            import statsmodels.api as sm

            x = sm.add_constant(both["index_ret"].to_numpy())
            fit = sm.OLS(both["ret"].to_numpy(), x).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
            m["alpha_daily"] = float(fit.params[0])
            m["alpha_annual"] = float(fit.params[0] * TRADING_DAYS_PER_YEAR)
            m["alpha_t"] = float(fit.tvalues[0])
            m["beta"] = float(fit.params[1])
            diff = (both["ret"] - both["index_ret"]).to_numpy()
            if diff.std(ddof=1) > 0:
                m["information_ratio"] = float(diff.mean() / diff.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        ie = daily["index_equity"].dropna()
        if len(ie):
            m["index_total_return"] = float(ie.iloc[-1] / initial - 1.0)
    m["exposure_avg"] = float(daily["exposure"].mean()) if len(daily) else float("nan")

    if signals is not None and len(signals):
        m["n_signals"] = int(len(signals))
        m["skipped_signals"] = int((signals["status"] == "skipped").sum())
        m["skipped_by_reason"] = signals.loc[signals["status"] == "skipped", "skip_reason"].value_counts().to_dict()
    else:
        m["n_signals"], m["skipped_signals"], m["skipped_by_reason"] = 0, 0, {}
    return m


# ------------------------------------------------------------------------------ walk-forward
def _metric_value(metrics: dict, name: str) -> float:
    v = metrics.get(name, float("nan"))
    return float(v) if v is not None and np.isfinite(v) else float("-inf")


def walk_forward(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    index: pd.DataFrame,
    param_grid: dict[str, Sequence],
    split_date,
    metric: str = "sharpe",
    base_config: BacktestConfig | None = None,
) -> dict:
    """Перебор параметров на in-sample (события до ``split_date``), проверка на out-of-sample.

    ``param_grid`` -- {поле BacktestConfig: список значений}, например
    ``{"hold_days": [5, 10, 20], "categories": [None, ("insider",)]}``. In-sample бэктест
    заканчивается на ``split_date`` (открытые позиции закрываются с ``exit_reason="end"``),
    out-of-sample начинается с ``split_date``. Возвращает dict: metric, split_date,
    best_params, in_sample / out_of_sample (метрики лучшего набора), grid (DataFrame по всем
    наборам с is_/oos_ метриками), results (BacktestResult лучшего набора для обеих частей).
    """
    base = base_config or BacktestConfig()
    split = pd.Timestamp(split_date)
    ev = normalize_events(events)
    ev_is = ev[ev["published_at"] < split]
    ev_oos = ev[ev["published_at"] >= split]
    panel = prepare_panel(prices, index, base)
    keys = list(param_grid)
    rows = []
    runs = []
    for combo in itertools.product(*[list(param_grid[k]) for k in keys]):
        params = dict(zip(keys, combo))
        cfg_is = replace(base, **params, end=split)
        cfg_oos = replace(base, **params, start=split)
        res_is = run_backtest(ev_is, prices, index, cfg_is, panel)
        res_oos = run_backtest(ev_oos, prices, index, cfg_oos, panel)
        runs.append((params, res_is, res_oos))
        rows.append({
            **{k: (v if not isinstance(v, (list, tuple)) else str(v)) for k, v in params.items()},
            f"is_{metric}": res_is.metrics.get(metric), f"oos_{metric}": res_oos.metrics.get(metric),
            "is_total_return": res_is.metrics["total_return"], "oos_total_return": res_oos.metrics["total_return"],
            "is_n_trades": res_is.metrics["n_trades"], "oos_n_trades": res_oos.metrics["n_trades"],
        })
    grid = pd.DataFrame(rows)
    if not runs:
        raise ValueError("Пустая сетка параметров")
    best_i = int(np.argmax([_metric_value(r[1].metrics, metric) for r in runs]))
    best_params, best_is, best_oos = runs[best_i]
    log.info("walk_forward: лучший набор %s, %s IS=%.3f OOS=%.3f", best_params, metric,
             _metric_value(best_is.metrics, metric), _metric_value(best_oos.metrics, metric))
    return {
        "metric": metric, "split_date": split, "best_params": best_params,
        "in_sample": best_is.metrics, "out_of_sample": best_oos.metrics, "grid": grid,
        "results": {"in_sample": best_is, "out_of_sample": best_oos},
    }


# ------------------------------------------------------------------------------------ placebo
def random_event_benchmark(
    events: pd.DataFrame,
    prices: pd.DataFrame,
    index: pd.DataFrame,
    config: BacktestConfig | None = None,
    n_iter: int = 200,
    random_state: int = 0,
    metrics: Iterable[str] = ("sharpe", "total_return"),
) -> dict:
    """Placebo-тест: те же бумаги и то же число сделок, но в случайные даты.

    Для каждой реальной сделки выбирается случайный торговый день с котировкой по той же
    бумаге (в диапазоне реального бэктеста, с запасом ``hold_days`` до конца) и создаётся
    событие с публикацией в 12:00 этого дня. Возвращает dict: real (метрики стратегии),
    placebo (DataFrame по итерациям), percentile (доля placebo-прогонов с метрикой ниже
    реальной; 0.5 за совпадение), n_iter.
    """
    config = config or BacktestConfig()
    metrics = list(metrics)
    panel = prepare_panel(prices, index, config)
    real = run_backtest(events, prices, index, config, panel)
    out = {"real": {k: real.metrics.get(k) for k in metrics}, "n_iter": 0, "percentile": {k: float("nan") for k in metrics},
           "placebo": pd.DataFrame(columns=["iter", *metrics, "n_trades"]), "n_trades": int(len(real.trades))}
    if real.trades.empty or not len(real.daily):
        log.warning("random_event_benchmark: реальная стратегия без сделок -- placebo не считается")
        return out
    rng = np.random.default_rng(random_state)
    cal = panel.calendar
    start_pos = int(cal.get_indexer([real.daily["date"].iloc[0]])[0])
    end_pos = int(cal.get_indexer([real.daily["date"].iloc[-1]])[0])
    last_entry = end_pos - config.hold_days - 2
    cfg = replace(config, start=cal[start_pos], end=cal[end_pos])
    col_map = panel.col_index
    candidates: dict[str, np.ndarray] = {}
    for s in real.trades["secid"].unique():
        valid = np.flatnonzero(np.isfinite(panel.close[:, col_map[s]]))
        candidates[s] = valid[(valid >= start_pos) & (valid <= last_entry)]
    base = real.trades[["secid", "category", "direction"]].reset_index(drop=True)
    rows = []
    for it in range(n_iter):
        days = np.array([rng.choice(candidates[s]) if len(candidates[s]) else -1 for s in base["secid"]])
        ok = days >= 0
        placebo = pd.DataFrame({
            "event_id": [f"pl{it}_{k}" for k in range(int(ok.sum()))],
            "secid": base.loc[ok, "secid"].to_numpy(),
            "published_at": cal.to_numpy()[days[ok]] + np.timedelta64(12, "h"),
            "category": base.loc[ok, "category"].to_numpy(),
            "direction": base.loc[ok, "direction"].to_numpy(),
            "size": np.nan,
        })
        res = run_backtest(placebo, prices, index, cfg, panel)
        rows.append({"iter": it, **{k: res.metrics.get(k) for k in metrics}, "n_trades": res.metrics["n_trades"]})
    placebo_df = pd.DataFrame(rows)
    out["placebo"] = placebo_df
    out["n_iter"] = int(n_iter)
    for k in metrics:
        real_v = real.metrics.get(k)
        vals = placebo_df[k].to_numpy(dtype=float)
        vals = vals[np.isfinite(vals)]
        if real_v is None or not np.isfinite(real_v) or not len(vals):
            continue
        out["percentile"][k] = float((vals < real_v).mean() + 0.5 * (vals == real_v).mean())
    return out


def config_to_dict(config: BacktestConfig) -> dict:
    """Конфигурация как dict (для отчётов)."""
    d = asdict(config)
    for k, v in d.items():
        if isinstance(v, pd.Timestamp):
            d[k] = str(v.date())
    return d
