"""Тесты модуля analysis на синтетических данных.

Панель: 40 бумаг x 600 торговых дней (рабочие дни), доходности = beta_i * f_t + eps_it,
индекс = среднее доходностей бумаг + шум. События категории "insider" получают
известный эффект: +0.5% (direction=+1) / -0.5% (direction=-1) аномальной доходности в день
на относительных днях 0..10; события "noise" эффекта не имеют. Две бумаги (S38, S39)
неликвидны (оборот ~1 млн руб.) для проверки фильтра ликвидности.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from disclosure_alpha.analysis.backtest import BacktestConfig, prepare_panel, random_event_benchmark, run_backtest, walk_forward
from disclosure_alpha.analysis.event_study import (
    EventStudyConfig,
    average_ar_path,
    benjamini_hochberg,
    calendar_time_portfolio,
    compute_car_table,
    screen_categories,
    signed_car,
    summarize,
)
from disclosure_alpha.analysis.report import df_to_markdown, plot_car_paths, plot_equity, write_markdown_report
from disclosure_alpha.analysis.returns import (
    index_returns,
    log_returns,
    market_adjusted_ar,
    market_model_ar,
    simple_returns,
    to_wide,
    trading_calendar,
)
from disclosure_alpha.analysis.timing import assign_event_day, assign_event_days, entry_day_next_open, entry_days_next_open

EFFECT = 0.005
EFFECT_DAYS = (0, 10)
ILLIQUID = ("S38", "S39")


# --------------------------------------------------------------------------- синтетика
def make_synthetic(seed=0, n_sec=40, n_days=600, n_insider=100, n_noise=100, effect=EFFECT, effect_days=EFFECT_DAYS,
                   illiquid=ILLIQUID, delist=None):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2022-01-03", periods=n_days)
    secids = [f"S{i:02d}" for i in range(n_sec)]
    beta = rng.normal(1.0, 0.3, n_sec)
    f = rng.normal(0.0003, 0.012, n_days)
    eps = rng.normal(0.0, 0.015, (n_days, n_sec))
    ret = beta[None, :] * f[:, None] + eps
    idx_ret = ret.mean(axis=1) + rng.normal(0.0, 0.002, n_days)

    def draw(n, category, prefix):
        sec_pos = rng.integers(0, n_sec, n)
        day_pos = rng.integers(60, n_days - 70, n)
        offset_h = rng.uniform(8.0, 60.0, n)  # часы от полуночи -> будни/выходные, до/после отсечки
        published = [days[p] + pd.Timedelta(hours=float(h)) for p, h in zip(day_pos, offset_h)]
        return pd.DataFrame({
            "event_id": [f"{prefix}{i:03d}" for i in range(n)],
            "secid": [secids[s] for s in sec_pos],
            "published_at": published,
            "category": category,
            "direction": rng.choice([1, -1], n),
            "size": rng.uniform(0.01, 2.0, n),
        })

    events = pd.concat([draw(n_insider, "insider", "ins"), draw(n_noise, "noise", "nz")], ignore_index=True)
    t0 = assign_event_days(events, days)
    t0_pos = days.get_indexer(pd.DatetimeIndex(t0))
    col = {s: i for i, s in enumerate(secids)}
    ins = events["category"] == "insider"
    for p, s, d in zip(t0_pos[ins], events.loc[ins, "secid"], events.loc[ins, "direction"]):
        for k in range(effect_days[0], effect_days[1] + 1):
            if 0 <= p + k < n_days:
                ret[p + k, col[s]] += d * effect

    p0 = rng.uniform(50, 500, n_sec)
    close = p0[None, :] * np.cumprod(1.0 + ret, axis=0)
    prev = np.vstack([p0[None, :], close[:-1]])
    open_ = prev * (1.0 + rng.normal(0, 0.003, close.shape))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.004, close.shape)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.004, close.shape)))
    turnover_level = np.full(n_sec, 2e7)
    for s in illiquid:
        turnover_level[col[s]] = 1e6
    value = turnover_level[None, :] * np.exp(rng.normal(0, 0.3, close.shape))
    volume = np.maximum((value / close).round(), 1)
    frames = [
        pd.DataFrame({"date": days.date, "secid": s, "open": open_[:, j], "high": high[:, j], "low": low[:, j],
                      "close": close[:, j], "volume": volume[:, j], "value": value[:, j]})
        for j, s in enumerate(secids)
    ]
    prices = pd.concat(frames, ignore_index=True)
    if delist:
        for s, last_day in delist.items():
            prices = prices[~((prices["secid"] == s) & (pd.to_datetime(prices["date"]) > pd.Timestamp(last_day)))]
    index = pd.DataFrame({"date": days.date, "close": 1000.0 * np.cumprod(1.0 + idx_ret)})
    return prices, index, events, days


@pytest.fixture(scope="module")
def synthetic():
    return make_synthetic()


@pytest.fixture(scope="module")
def es_config():
    return EventStudyConfig(bootstrap_iter=200, random_state=0)


@pytest.fixture(scope="module")
def car_result(synthetic, es_config):
    prices, index, events, _ = synthetic
    return compute_car_table(events, prices, index, es_config)


@pytest.fixture(scope="module")
def bt_config():
    return BacktestConfig(hold_days=10, categories=("insider",), direction_filter=(1,))


@pytest.fixture(scope="module")
def bt_result(synthetic, bt_config):
    prices, index, events, _ = synthetic
    return run_backtest(events, prices, index, bt_config)


# ------------------------------------------------------------------------------ timing
@pytest.fixture(scope="module")
def calendar_2022():
    return pd.bdate_range("2022-01-03", "2022-12-30").difference(pd.DatetimeIndex(["2022-03-08"]))  # 8 марта -- праздник


@pytest.mark.parametrize("published, expected", [
    ("2022-03-01 12:00", "2022-03-01"),      # вторник, до отсечки -> тот же день
    ("2022-03-01 18:39:59", "2022-03-01"),   # строго до 18:40
    ("2022-03-01 18:40", "2022-03-02"),      # ровно в отсечку -> следующий день
    ("2022-03-01 21:15", "2022-03-02"),      # после отсечки
    ("2022-03-04 19:00", "2022-03-07"),      # пятница вечером -> понедельник
    ("2022-03-05 11:00", "2022-03-07"),      # суббота -> понедельник
    ("2022-03-08 12:00", "2022-03-09"),      # праздник -> следующий торговый день
    ("2022-03-07 19:00", "2022-03-09"),      # вечер перед праздником -> перескакивает праздник
])
def test_assign_event_day_rules(calendar_2022, published, expected):
    assert assign_event_day(pd.Timestamp(published), calendar_2022) == pd.Timestamp(expected)


def test_assign_event_day_edge_cases(calendar_2022):
    assert pd.isna(assign_event_day("2022-12-30 19:00", calendar_2022))  # после последнего дня календаря
    assert pd.isna(assign_event_day(None, calendar_2022))
    assert assign_event_day("2022-03-01 12:00", calendar_2022, cutoff_time="11:00") == pd.Timestamp("2022-03-02")
    df = pd.DataFrame({"published_at": pd.to_datetime(["2022-03-01 12:00", "2022-03-05 11:00", "2022-03-08 12:00", None])})
    vec = assign_event_days(df, calendar_2022)
    assert vec.dtype == "datetime64[ns]" and list(vec.index) == [0, 1, 2, 3]
    assert vec.iloc[:3].tolist() == [pd.Timestamp("2022-03-01"), pd.Timestamp("2022-03-07"), pd.Timestamp("2022-03-09")]
    assert pd.isna(vec.iloc[3])


@pytest.mark.parametrize("published, expected", [
    ("2022-03-01 09:30", "2022-03-01"),   # до открытия -> вход в этот день
    ("2022-03-01 10:00", "2022-03-02"),   # в момент открытия -> следующая сессия
    ("2022-03-01 12:00", "2022-03-02"),
    ("2022-03-04 12:00", "2022-03-07"),   # пятница -> понедельник
    ("2022-03-05 09:00", "2022-03-07"),   # суббота
    ("2022-03-07 12:00", "2022-03-09"),   # перед праздником
])
def test_entry_day_next_open_rules(calendar_2022, published, expected):
    assert entry_day_next_open(pd.Timestamp(published), calendar_2022) == pd.Timestamp(expected)
    vec = entry_days_next_open(pd.Series([pd.Timestamp(published)]), calendar_2022)
    assert vec.iloc[0] == pd.Timestamp(expected)


# ----------------------------------------------------------------------------- returns
def test_to_wide_and_returns(synthetic):
    prices, index, _, days = synthetic
    prices = prices.copy()
    gap_day = days[100].date()
    prices.loc[(prices["secid"] == "S01") & (prices["date"] == gap_day), "close"] = np.nan
    wide = to_wide(prices)
    assert wide.shape == (600, 40) and wide.index.dtype == "datetime64[ns]" and list(wide.columns)[:2] == ["S00", "S01"]
    assert np.isnan(wide.loc[days[100], "S01"])  # пропуск не заполняется
    r = simple_returns(wide)
    assert np.isnan(r.loc[days[100], "S01"])
    expected = wide.loc[days[101], "S01"] / wide.loc[days[99], "S01"] - 1  # доходность через пропуск
    assert r.loc[days[101], "S01"] == pytest.approx(expected)
    assert np.isnan(simple_returns(wide, fill_gaps=False).loc[days[101], "S01"])
    lr = log_returns(wide)
    assert np.allclose(np.log1p(r.iloc[1:, 0]), lr.iloc[1:, 0])
    cal = trading_calendar(prices, index)
    mkt = index_returns(index, cal)
    ar = market_adjusted_ar(r, mkt)
    assert ar.iloc[5, 3] == pytest.approx(r.iloc[5, 3] - mkt.iloc[5])


def test_market_model_fallback(synthetic):
    prices, index, _, days = synthetic
    wide = to_wide(prices)
    cal = trading_calendar(prices, index)
    ret = simple_returns(wide.reindex(cal))
    mkt = index_returns(index, cal)
    ev = pd.DataFrame({"event_id": ["short", "full", "unknown"], "secid": ["S03", "S03", "ZZZ"], "t0": [days[40], days[400], days[400]]})
    res = market_model_ar(ret, mkt, ev, est_window=(-250, -30), min_obs=100, event_window=(-2, 5))
    p = res.params
    assert p.loc["short", "model"] == "market_adjusted" and p.loc["short", "beta"] == 1.0 and p.loc["short", "alpha"] == 0.0
    assert p.loc["full", "model"] == "market_model" and p.loc["full", "n_obs"] == 221 and 0.3 < p.loc["full", "beta"] < 2.0
    assert p.loc["unknown", "model"] == "market_adjusted" and res.ar.loc["unknown"].isna().all()
    # для отката AR = r - r_m; для рыночной модели AR = r - alpha - beta r_m
    raw, m = res.raw.loc["short", 0], res.mkt.loc["short", 0]
    assert res.ar.loc["short", 0] == pytest.approx(raw - m)
    a, b = p.loc["full", "alpha"], p.loc["full", "beta"]
    assert res.ar.loc["full", 3] == pytest.approx(res.raw.loc["full", 3] - a - b * res.mkt.loc["full", 3])
    # вариант dict secid -> t0
    res2 = market_model_ar(ret, mkt, {"S03": days[400]}, event_window=(0, 1))
    assert res2.params.loc["S03", "beta"] == pytest.approx(b)


# --------------------------------------------------------------------------- event study
def test_event_study_detects_injected_effect(car_result):
    table, diag = car_result
    assert set(["event_id", "secid", "category", "direction", "t0", "window", "car", "bhar", "n_days", "raw_ret", "mkt_ret", "model"]) <= set(table.columns)
    assert diag["n_input"] == 200 and diag["dropped_illiquid"] > 0 and diag["n_used"] == 200 - diag["dropped_illiquid"]
    assert not table["secid"].isin(ILLIQUID).any()
    assert (table["model"] == "market_adjusted").all()
    summ = summarize(signed_car(table), by=("category", "window"), value_col="signed_car", bootstrap_iter=200)
    row = summ.set_index(["category", "window"])
    ins = row.loc[("insider", "[0,10]")]
    noise = row.loc[("noise", "[0,10]")]
    assert ins["n"] >= 80 and noise["n"] >= 80
    assert ins["t_stat"] > 3 and ins["p_value"] < 1e-3 and ins["sign_test_p"] < 1e-3
    assert 0.03 < ins["mean_car"] < 0.08 and 0.03 < ins["mean_bhar"] < 0.08
    assert ins["ci_low"] > 0 and ins["ci_low"] < ins["mean_car"] < ins["ci_high"]
    assert abs(noise["t_stat"]) < 2 and noise["ci_low"] < 0 < noise["ci_high"]
    assert abs(row.loc[("insider", "[-5,-1]"), "t_stat"]) < 3  # до события эффекта нет
    # покупки и продажи по отдельности: знак CAR совпадает с направлением сделки
    by_dir = summarize(table, by=("category", "direction", "window"), bootstrap_iter=50).set_index(["category", "direction", "window"])
    assert by_dir.loc[("insider", 1, "[0,10]"), "mean_car"] > 0.02 and by_dir.loc[("insider", -1, "[0,10]"), "mean_car"] < -0.02


def test_market_model_event_study(synthetic):
    prices, index, events, _ = synthetic
    table, diag = compute_car_table(events, prices, index, EventStudyConfig(model="market_model", bootstrap_iter=50))
    assert set(table["model"].unique()) == {"market_model", "market_adjusted"}  # ранние события -- откат
    summ = summarize(signed_car(table), by=("category", "window"), value_col="signed_car", bootstrap_iter=50).set_index(["category", "window"])
    assert summ.loc[("insider", "[0,10]"), "t_stat"] > 3 and abs(summ.loc[("noise", "[0,10]"), "t_stat"]) < 2


def test_screen_categories(car_result):
    table, _ = car_result
    summ = summarize(signed_car(table), by=("category", "window"), value_col="signed_car", bootstrap_iter=50)
    for window in [(0, 10), (0, 20)]:
        scr = screen_categories(summ, window=window, min_n=30, alpha=0.05).set_index("category")
        assert bool(scr.loc["insider", "significant"]) and not bool(scr.loc["noise", "significant"])
        assert scr.loc["insider", "rank"] == 1 and scr.loc["insider", "q_value"] < 0.05 <= scr.loc["noise", "q_value"]
    assert screen_categories(summ, window=(0, 10), min_n=1000).empty
    q = benjamini_hochberg(np.array([0.01, 0.04, 0.03, 0.5]))
    assert np.allclose(q, [0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.5])


def test_average_ar_path(synthetic, es_config):
    prices, index, events, _ = synthetic
    path = average_ar_path(events, prices, index, es_config, start=-5, end=15, signed=True)
    assert list(path.columns) == ["category", "direction", "rel_day", "mean_ar", "mean_car", "n"]
    assert set(path["direction"]) == {"signed"} and path["rel_day"].min() == -5 and path["rel_day"].max() == 15
    ins = path[path["category"] == "insider"].set_index("rel_day")
    noise = path[path["category"] == "noise"].set_index("rel_day")
    assert ins.loc[10, "mean_car"] - ins.loc[-1, "mean_car"] > 0.03 and ins["n"].min() >= 80
    assert (ins.loc[0:10, "mean_ar"] > 0).mean() >= 0.9  # дрейф на днях 0..10
    assert abs(noise.loc[10, "mean_car"] - noise.loc[-1, "mean_car"]) < 0.02
    unsigned = average_ar_path(events, prices, index, es_config, start=-2, end=2)
    assert set(unsigned["direction"]) == {1, -1}


def test_calendar_time_portfolio(synthetic):
    prices, index, events, _ = synthetic
    res = calendar_time_portfolio(events, prices, index, hold_days=10, direction_filter=+1, categories=["insider"])
    daily, st = res
    assert list(daily.columns) == ["date", "port_ret", "mkt_ret", "n_stocks"] and st["n_days"] > 100
    assert st["alpha"] > 0.002 and st["alpha_t"] > 3 and 0.5 < st["beta"] < 1.5
    noise = calendar_time_portfolio(events, prices, index, hold_days=10, direction_filter=+1, categories=["noise"]).stats
    assert abs(noise["alpha_t"]) < 2.5
    short = calendar_time_portfolio(events, prices, index, hold_days=10, direction_filter=-1, categories=["insider"]).stats
    assert short["alpha"] < 0


# ------------------------------------------------------------------------------ backtest
def test_backtest_accounting(synthetic, bt_config, bt_result):
    prices, index, events, _ = synthetic
    res = bt_result
    trades, daily, m = res.trades, res.daily, res.metrics
    n_signals = int(((events["category"] == "insider") & (events["direction"] == 1)).sum())
    assert m["n_signals"] == n_signals and 0 < m["n_trades"] <= n_signals
    assert m["n_trades"] + m["skipped_signals"] == n_signals and "illiquid" in m["skipped_by_reason"]
    assert m["total_return"] > 0 and m["sharpe"] > 1 and m["hit_rate"] > 0.5 and m["profit_factor"] > 1
    assert m["max_drawdown"] <= 0 and m["exposure_avg"] > 0 and np.isfinite(m["alpha_t"]) and np.isfinite(m["beta"])
    # издержки: net_ret получается из gross_ret с учётом проскальзывания и комиссии
    s, c = bt_config.slippage_bps / 1e4, bt_config.commission_bps / 1e4
    g = trades["gross_ret"]
    expected_net = (1 + g) * (1 - s) / (1 + s) - 1 - c * (1 + (1 + g) * (1 - s) / (1 + s))
    assert np.allclose(trades["net_ret"], expected_net, atol=1e-9)
    assert (trades["gross_ret"] - trades["net_ret"]).between(0.002, 0.005).all()  # ~2*10 б.п. + 2*5 б.п. (x (1+g))
    assert np.allclose(trades["pnl"], trades["net_ret"] * trades["notional"])
    # учёт капитала: все позиции закрыты, equity = cash, прирост = сумма pnl
    assert daily["n_positions"].iloc[-1] == 0 and daily["equity"].iloc[-1] == pytest.approx(daily["cash"].iloc[-1])
    assert daily["equity"].iloc[-1] - bt_config.initial_capital == pytest.approx(trades["pnl"].sum())
    assert (daily["equity"] >= daily["cash"] - 1e-6).all()  # длинные позиции: стоимость >= 0
    assert np.allclose(daily["ret"].iloc[1:], daily["equity"].pct_change().iloc[1:])
    # без заглядывания в будущее: вход не раньше следующего открытия после публикации
    pub = events.set_index("event_id")["published_at"]
    for _, t in trades.iterrows():
        p = pub[t["event_id"]]
        assert t["entry_date"] >= p.normalize()
        if p.time() >= pd.Timestamp("10:00").time():
            assert t["entry_date"] > p.normalize()
        assert t["exit_date"] > t["entry_date"]
    assert (trades.loc[trades["exit_reason"] == "hold_days", "hold_days"] == bt_config.hold_days).all()
    assert (trades["qty"] >= 1).all() and (trades["qty"] == trades["qty"].astype(int)).all()
    assert (trades["notional"] <= bt_config.initial_capital / bt_config.max_positions * 1.5).all()


def test_backtest_end_delisting_stoploss(synthetic):
    _, _, events, days = synthetic
    prices2, index2, events2, _ = make_synthetic(delist={"S05": days[300]})
    extra = pd.DataFrame({
        "event_id": ["dl1", "end1"], "secid": ["S05", "S01"],
        "published_at": [days[295] + pd.Timedelta(hours=12), days[595] + pd.Timedelta(hours=12)],
        "category": "insider", "direction": 1, "size": np.nan,
    })
    cfg = BacktestConfig(hold_days=10, categories=("insider",), stop_loss=0.05, allow_multiple_per_secid=True)
    res = run_backtest(pd.concat([events2, extra], ignore_index=True), prices2, index2, cfg)
    tr = res.trades.set_index("event_id")
    last_price_day = pd.Timestamp(prices2.loc[prices2["secid"] == "S05", "date"].max())
    assert tr.loc["dl1", "exit_reason"] == "delisted" and tr.loc["dl1", "exit_date"] == last_price_day
    assert tr.loc["end1", "exit_reason"] == "end" and tr.loc["end1", "exit_date"] == days[-1] and tr.loc["end1", "hold_days"] < 10
    assert res.daily["date"].iloc[-1] == days[-1] and res.daily["n_positions"].iloc[-1] == 0
    assert res.metrics["exit_reasons"].get("stop_loss", 0) > 0
    sl = res.trades[res.trades["exit_reason"] == "stop_loss"]
    assert (sl["gross_ret"] <= -0.05 + 0.002).all() and (sl["hold_days"] < 10).all()
    assert res.daily["equity"].iloc[-1] - cfg.initial_capital == pytest.approx(res.trades["pnl"].sum())


def test_backtest_shorts_and_entry_modes(synthetic):
    prices, index, events, _ = synthetic
    cfg = BacktestConfig(hold_days=10, categories=("insider",), allow_short=True, direction_filter=(1, -1))
    res = run_backtest(events, prices, index, cfg)
    assert set(res.trades["side"]) == {1, -1}
    shorts = res.trades[res.trades["side"] == -1]
    assert (shorts["entry_price"] < shorts["entry_price_raw"]).all() and (shorts["exit_price"] > shorts["exit_price_raw"]).all()
    assert np.allclose(shorts["gross_ret"], -(shorts["exit_price_raw"] / shorts["entry_price_raw"] - 1))
    assert res.daily["equity"].iloc[-1] - cfg.initial_capital == pytest.approx(res.trades["pnl"].sum())
    assert res.metrics["total_return"] > 0  # продажи инсайдеров дают отрицательный дрейф -> шорт прибылен
    no_short = run_backtest(events, prices, index, BacktestConfig(hold_days=10, categories=("insider",), direction_filter=(1, -1)))
    assert no_short.metrics["skipped_by_reason"].get("short_not_allowed", 0) > 0 and set(no_short.trades["side"]) == {1}
    for entry in ("t0_close", "next_close"):
        r = run_backtest(events, prices, index, BacktestConfig(hold_days=10, categories=("insider",), entry=entry))
        assert r.metrics["n_trades"] > 0 and r.metrics["total_return"] > 0
        assert (r.trades["entry_date"] > events.set_index("event_id").loc[r.trades["event_id"], "published_at"].dt.normalize().to_numpy() - pd.Timedelta(days=1)).all()
    empty = run_backtest(events.iloc[:0], prices, index, cfg)
    assert empty.metrics["n_trades"] == 0 and empty.trades.empty and len(empty.daily) == 600


def test_random_event_benchmark(synthetic, bt_config):
    prices, index, events, _ = synthetic
    pb = random_event_benchmark(events, prices, index, bt_config, n_iter=30, random_state=1)
    assert pb["n_iter"] == 30 and len(pb["placebo"]) == 30
    assert pb["percentile"]["sharpe"] > 0.9 and pb["percentile"]["total_return"] > 0.9
    assert (pb["placebo"]["n_trades"] <= pb["n_trades"]).all() and pb["placebo"]["n_trades"].min() >= pb["n_trades"] - 5
    assert pb["real"]["sharpe"] > pb["placebo"]["sharpe"].quantile(0.9)


def test_walk_forward(synthetic, bt_config):
    prices, index, events, days = synthetic
    wf = walk_forward(events, prices, index, {"hold_days": [5, 10], "categories": [("insider",), ("noise",)]},
                      split_date=days[350], metric="sharpe", base_config=bt_config)
    assert set(wf) >= {"metric", "split_date", "best_params", "in_sample", "out_of_sample", "grid", "results"}
    assert len(wf["grid"]) == 4 and {"hold_days", "categories", "is_sharpe", "oos_sharpe", "is_n_trades", "oos_n_trades"} <= set(wf["grid"].columns)
    assert wf["best_params"]["categories"] == ("insider",)
    for part in ("in_sample", "out_of_sample"):
        assert "sharpe" in wf[part] and wf[part]["n_trades"] > 0
    assert wf["in_sample"]["sharpe"] == wf["grid"]["is_sharpe"].max()
    res_is, res_oos = wf["results"]["in_sample"], wf["results"]["out_of_sample"]
    assert res_is.daily["date"].iloc[-1] <= days[350] and res_oos.daily["date"].iloc[0] >= days[350]
    assert (res_is.trades["exit_date"] <= days[350]).all() and (res_oos.trades["entry_date"] >= days[350]).all()


def test_prepare_panel_reuse(synthetic, bt_config, bt_result):
    prices, index, events, _ = synthetic
    panel = prepare_panel(prices, index, bt_config)
    res = run_backtest(events, prices, index, bt_config, panel=panel)
    assert res.metrics["total_return"] == pytest.approx(bt_result.metrics["total_return"])


# -------------------------------------------------------------------------------- report
def test_report_writer(tmp_path, car_result, bt_result, synthetic, es_config):
    table, diag = car_result
    prices, index, events, _ = synthetic
    summ = summarize(table, bootstrap_iter=20)
    md = df_to_markdown(summ.head(3))
    assert md.startswith("| category |") and md.count("\n") == 4 and "|---" in md.splitlines()[1]
    assert df_to_markdown(pd.DataFrame({"x": [1.23456789, np.nan], "s": ["a|b", None]})).splitlines()[2] == "| 1.2346 | a\\|b |"
    path = write_markdown_report(tmp_path / "report.md", [("Сводка", summ.head(5)), ("Метрики", bt_result.metrics), ("Примечание", "текст")], title="Отчёт")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Отчёт") and "## Сводка" in text and "| category |" in text and "|---" in text and "текст" in text
    assert "| total_return |" in text
    car_path = average_ar_path(events, prices, index, es_config, start=-10, end=20)
    png1 = plot_car_paths(car_path, tmp_path / "car.png", title="CAR")
    png2 = plot_equity(bt_result.daily, tmp_path / "equity.png")
    assert png1.exists() and png1.stat().st_size > 1000 and png2.exists() and png2.stat().st_size > 1000
