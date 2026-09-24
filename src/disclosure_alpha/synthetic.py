"""Синтетические данные для сквозной проверки конвейера без сети (команда `demo`).

Генерирует панель цен (GBM), индекс, таблицу событий нескольких категорий и «вшивает» известный эффект:
после инсайдерских покупок +drift в день на горизонте hold, после продаж -- симметрично; прочие категории без эффекта.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd


def make_synthetic(n_secids: int = 40, n_days: int = 750, n_events: int = 400, effect_per_day: float = 0.004,
                   effect_days: int = 10, seed: int = 0, start: str = "2021-01-04"):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, periods=n_days)
    secids = [f"SYN{i:02d}" for i in range(n_secids)]
    mkt = rng.normal(0.0003, 0.012, n_days)
    betas = rng.uniform(0.6, 1.4, n_secids)
    idio = rng.normal(0, 0.018, (n_days, n_secids))
    rets = mkt[:, None] * betas[None, :] + idio

    cats = ["insider_stake_change", "buyback", "board_agenda", "rating"]
    ev_rows = []
    for k in range(n_events):
        j = int(rng.integers(0, n_secids))
        t = int(rng.integers(30, n_days - 80))
        cat = cats[k % len(cats)]
        if cat == "insider_stake_change":
            direction = 1 if rng.random() < 0.6 else -1
            rets[t + 1: t + 1 + effect_days, j] += direction * effect_per_day
            size = abs(rng.normal(0.01, 0.005))
        elif cat == "buyback":
            direction = 1
            size = np.nan
        else:
            direction = 0
            size = np.nan
        published = datetime.combine(days[t].date(), datetime.min.time()) + timedelta(hours=int(rng.integers(9, 20)), minutes=int(rng.integers(0, 60)))
        ev_rows.append({"event_id": f"SYN-{k:05d}", "secid": secids[j], "published_at": published,
                        "category": cat, "direction": direction, "size": size, "company_id": 1000 + j,
                        "role": rng.choice(["ceo", "board", "management_board"]), "has_confounder": False})
    events = pd.DataFrame(ev_rows).sort_values("published_at").reset_index(drop=True)

    prices = 100 * np.exp(np.cumsum(rets, axis=0))
    rows = []
    for j, s in enumerate(secids):
        close = prices[:, j]
        open_ = close * (1 + rng.normal(0, 0.003, n_days))
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n_days)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n_days)))
        vol = rng.integers(50_000, 500_000, n_days)
        rows.append(pd.DataFrame({"date": days.date, "secid": s, "open": open_, "high": high, "low": low,
                                  "close": close, "volume": vol, "value": vol * close}))
    panel = pd.concat(rows, ignore_index=True)
    index = pd.DataFrame({"date": days.date, "close": 1000 * np.exp(np.cumsum(mkt))})
    return events, panel, index
