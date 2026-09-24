"""Конвейер: вселенная эмитентов → сообщения → события/сделки инсайдеров → таблица событий → исследование.

Каждый шаг идемпотентен и опирается на файлы в data/processed (parquet); повторный запуск дозагружает
только новое. Модули анализа импортируются лениво, чтобы сбор данных не зависел от них.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd

from .config import Settings
from .edisclosure.client import EDisclosureClient
from .edisclosure.insider import classify_role, is_stake_change_text, parse_stake_change
from .edisclosure.store import DisclosureStore
from .edisclosure.taxonomy import Taxonomy, default_taxonomy
from .models import CompanyInfo, EVENT_COLUMNS

log = logging.getLogger(__name__)

CONFOUNDER_CATEGORIES = ("buyback", "dividends_recommendation", "dividends_decision", "major_holder_change", "significant_other")


# ----------------------------------------------------------------------------- вселенная эмитентов
def build_universe(settings: Settings, ed: EDisclosureClient, iss=None, limit: Optional[int] = None,
                   resolve_edisclosure_ids: bool = True) -> pd.DataFrame:
    """Список акций MOEX (включая делистингованные) + сопоставление ИНН -> id компании на e-disclosure.

    Сохраняет data/processed/securities.parquet, mapping.parquet, companies.parquet.
    """
    from .moex.iss import IssClient
    from .moex.mapping import build_inn_to_secids

    settings.ensure_dirs()
    iss = iss or IssClient(settings)
    secs = iss.list_all_shares()
    secs.to_parquet(settings.processed_dir / "securities.parquet", index=False)
    mapping = build_inn_to_secids(secs)
    mapping.to_parquet(settings.processed_dir / "mapping.parquet", index=False)
    log.info("бумаг в справочнике ISS: %d, уникальных ИНН: %d", len(secs), mapping["inn"].nunique())

    store = DisclosureStore(settings)
    if resolve_edisclosure_ids:
        known = store.load_companies()
        known_inns = set(known["inn"].dropna().astype(str)) if len(known) else set()
        inns = [i for i in mapping.loc[mapping["primary"], "inn"].dropna().unique() if i not in known_inns]
        if limit:
            inns = inns[:limit]
        found: list[CompanyInfo] = []
        for i, inn in enumerate(inns, 1):
            try:
                cands = ed.find_companies(inn)
            except Exception as e:  # noqa: BLE001 -- сбор не должен падать из-за одной компании
                log.warning("поиск компании по ИНН %s: %s", inn, e)
                continue
            for c in cands:
                if c.inn is None:
                    try:
                        c = ed.get_company(c.company_id)
                    except Exception as e:  # noqa: BLE001
                        log.warning("страница компании %s: %s", c.company_id, e)
                        continue
                if c.inn == inn:
                    found.append(c)
                    break
            if i % 25 == 0:
                log.info("сопоставлено компаний: %d/%d", i, len(inns))
        if found:
            store.upsert_companies(found)
    return store.load_companies()


# ----------------------------------------------------------------------------- сообщения
def resolve_event_type_ids(ed: EDisclosureClient, taxonomy: Taxonomy, categories: Iterable[str]) -> list[str]:
    """Идентификаторы чекбоксов формы, чьи подписи попадают в заданные категории таксономии."""
    wanted = set(categories)
    ids = []
    for opt in ed.event_type_options():
        if taxonomy.classify_name(opt.get("label", "")) in wanted:
            ids.append(str(opt["value"]))
    return ids


def collect_messages(settings: Settings, ed: EDisclosureClient, date_from: date, date_till: date,
                     categories: Optional[Iterable[str]] = None, company_ids: Optional[Iterable[int]] = None,
                     chunk_days: int = 1, taxonomy: Optional[Taxonomy] = None) -> pd.DataFrame:
    """Сбор строк поиска за период: глобально (с фильтром по типам) или по списку компаний."""
    taxonomy = taxonomy or default_taxonomy()
    store = DisclosureStore(settings)
    type_ids = resolve_event_type_ids(ed, taxonomy, categories) if categories else None
    if categories and not type_ids:
        log.warning("не удалось сопоставить категории %s с типами формы -- ищем без фильтра по типу", list(categories))
    rows = []
    if company_ids:
        for cid in company_ids:
            rows.extend(ed.iter_search(date_from, date_till, chunk_days=max(chunk_days, 366),
                                       event_type_ids=type_ids, query_id=int(cid)))
    else:
        rows.extend(ed.iter_search(date_from, date_till, chunk_days=chunk_days, event_type_ids=type_ids))
    if categories:
        wanted = set(categories)
        rows = [r for r in rows if taxonomy.classify_name(r.event_type) in wanted]
    return store.append_messages(rows)


def ensure_company_info(settings: Settings, ed: EDisclosureClient, company_ids: Iterable[int]) -> pd.DataFrame:
    """Догружает страницы компаний (ИНН/ОГРН) для тех id, которых ещё нет в companies.parquet."""
    store = DisclosureStore(settings)
    known = store.load_companies()
    have = set(known["company_id"].dropna().astype(int)) if len(known) else set()
    todo = [int(c) for c in pd.unique(pd.Series(list(company_ids)).dropna()) if int(c) not in have]
    infos = []
    for i, cid in enumerate(todo, 1):
        try:
            infos.append(ed.get_company(cid))
        except Exception as e:  # noqa: BLE001
            log.warning("компания %s: %s", cid, e)
        if i % 50 == 0:
            log.info("страниц компаний загружено: %d/%d", i, len(todo))
    if infos:
        store.upsert_companies(infos)
    return store.load_companies()


# ----------------------------------------------------------------------------- события и сделки инсайдеров
def fetch_events(settings: Settings, ed: EDisclosureClient, categories: Iterable[str] = ("insider_stake_change",),
                 limit: Optional[int] = None, only_mapped: bool = True, taxonomy: Optional[Taxonomy] = None) -> pd.DataFrame:
    """Загружает страницы сообщений выбранных категорий и разбирает сделки инсайдеров.

    Возвращает таблицу insider_trades (одна строка на сообщение) и сохраняет её в processed/insider_trades.parquet.
    """
    taxonomy = taxonomy or default_taxonomy()
    store = DisclosureStore(settings)
    msgs = store.load_messages()
    if not len(msgs):
        log.warning("сообщений нет -- сначала выполните сбор (messages)")
        return pd.DataFrame()
    msgs = msgs.assign(category=msgs["event_type"].map(taxonomy.classify_name))
    sel = msgs[msgs["category"].isin(set(categories))].copy()
    if only_mapped:
        mapped_ids = _mapped_company_ids(settings)
        if mapped_ids is not None:
            sel = sel[sel["company_id"].isin(mapped_ids)]
    if limit:
        sel = sel.head(limit)
    log.info("событий к загрузке: %d", len(sel))

    records = []
    for i, m in enumerate(sel.itertuples(index=False), 1):
        ev = store.load_event(m.event_id)
        if ev is None:
            try:
                ev = ed.get_event(m.event_id)
            except Exception as e:  # noqa: BLE001
                log.warning("событие %s: %s", m.event_id, e)
                continue
            if not ev.published_at and m.published_at is not None and not pd.isna(m.published_at):
                ev.published_at = pd.Timestamp(m.published_at).to_pydatetime()
            if not ev.company_id and m.company_id is not None and not pd.isna(m.company_id):
                ev.company_id = int(m.company_id)
            store.save_event(ev)
        rec = {"event_id": m.event_id, "company_id": ev.company_id or m.company_id, "published_at": m.published_at,
               "category": m.category, "event_type": m.event_type}
        if m.category == "insider_stake_change" or is_stake_change_text(ev.body_text):
            sc = parse_stake_change(ev.body_text, m.event_id)
            rec.update({k: v for k, v in sc.to_dict().items() if k != "event_id"})
            rec["role"] = classify_role(sc.position)
        records.append(rec)
        if i % 100 == 0:
            log.info("событий обработано: %d/%d", i, len(sel))
    trades = pd.DataFrame(records)
    if len(trades):
        trades.to_parquet(settings.processed_dir / "insider_trades.parquet", index=False)
    return trades


def _mapped_company_ids(settings: Settings) -> Optional[set]:
    companies = DisclosureStore(settings).load_companies()
    mp = settings.processed_dir / "mapping.parquet"
    if not len(companies) or not mp.exists():
        return None
    mapping = pd.read_parquet(mp)
    inns = set(mapping["inn"].dropna().astype(str))
    return set(companies.loc[companies["inn"].astype(str).isin(inns), "company_id"].astype(int))


# ----------------------------------------------------------------------------- таблица событий
def build_events_table(settings: Settings, categories: Optional[Iterable[str]] = None,
                       taxonomy: Optional[Taxonomy] = None, exclude_subsidiary: bool = True) -> pd.DataFrame:
    """Собирает вход для event study: event_id, secid, published_at, category, direction, size + доп. поля.

    direction: для insider_stake_change -- знак изменения доли (из insider_trades), для остальных -- статическое
    правило категории (buyback +1, default -1, иначе 0).
    """
    taxonomy = taxonomy or default_taxonomy()
    store = DisclosureStore(settings)
    msgs = store.load_messages()
    if not len(msgs):
        return pd.DataFrame(columns=EVENT_COLUMNS)
    msgs = msgs.assign(category=msgs["event_type"].map(taxonomy.classify_name))
    if categories:
        msgs = msgs[msgs["category"].isin(set(categories))]

    companies = store.load_companies()
    mapping = pd.read_parquet(settings.processed_dir / "mapping.parquet")
    prim = mapping[mapping["primary"]][["inn", "secid"]].drop_duplicates("inn")
    comp = companies[["company_id", "inn"]].dropna().astype({"company_id": int, "inn": str}).merge(prim, on="inn", how="inner")
    ev = msgs.merge(comp[["company_id", "secid"]], on="company_id", how="inner")
    ev["direction"] = ev["category"].map(taxonomy.static_direction).astype(int)
    ev["size"] = np.nan

    tp = settings.processed_dir / "insider_trades.parquet"
    if tp.exists():
        tr = pd.read_parquet(tp)
        keep = [c for c in ("event_id", "person", "position", "role", "is_subsidiary", "share_before", "share_after",
                            "change_date", "delta_pp") if c in tr.columns]
        tr = tr[keep + ["direction"]].rename(columns={"direction": "trade_direction"})
        ev = ev.merge(tr, on="event_id", how="left")
        ins = ev["category"] == "insider_stake_change"
        ev.loc[ins, "direction"] = ev.loc[ins, "trade_direction"].fillna(0).astype(int)
        ev.loc[ins, "size"] = ev.loc[ins, "delta_pp"]
        if exclude_subsidiary and "is_subsidiary" in ev.columns:
            ev = ev[~(ins & ev["is_subsidiary"].fillna(False).astype(bool))]
        ev = ev.drop(columns=["trade_direction"])

    ev["published_at"] = pd.to_datetime(ev["published_at"])
    ev = ev.dropna(subset=["published_at"]).sort_values("published_at").reset_index(drop=True)
    ev = _flag_confounders(ev)
    ev.to_parquet(settings.processed_dir / "events.parquet", index=False)
    log.info("таблица событий: %d строк, %d бумаг, категории: %s", len(ev), ev["secid"].nunique(),
             ev["category"].value_counts().head(10).to_dict())
    return ev


def _flag_confounders(ev: pd.DataFrame, window_days: int = 5) -> pd.DataFrame:
    """Помечает события, рядом с которыми (±window_days) у той же бумаги есть сообщения категорий-конфаундеров."""
    ev = ev.copy()
    ev["has_confounder"] = False
    conf = ev[ev["category"].isin(CONFOUNDER_CATEGORIES)][["secid", "published_at"]]
    if not len(conf):
        return ev
    conf_by_sec = {s: g["published_at"].values for s, g in conf.groupby("secid")}
    delta = np.timedelta64(window_days, "D")
    flags = []
    for r in ev.itertuples(index=False):
        arr = conf_by_sec.get(r.secid)
        if arr is None:
            flags.append(False)
            continue
        t = np.datetime64(r.published_at)
        near = (np.abs(arr - t) <= delta)
        # само событие-конфаундер не считаем своим же соседом
        flags.append(bool(near.sum() - (1 if r.category in CONFOUNDER_CATEGORIES else 0) > 0))
    ev["has_confounder"] = flags
    return ev


# ----------------------------------------------------------------------------- цены
def update_prices(settings: Settings, date_from: date, date_till: date, secids: Optional[Iterable[str]] = None, iss=None) -> pd.DataFrame:
    from .moex.iss import IssClient
    from .moex.prices import PriceStore

    iss = iss or IssClient(settings)
    ps = PriceStore(settings)
    if secids is None:
        ev_path = settings.processed_dir / "events.parquet"
        if ev_path.exists():
            secids = pd.read_parquet(ev_path)["secid"].unique().tolist()
        else:
            mapping = pd.read_parquet(settings.processed_dir / "mapping.parquet")
            secids = mapping.loc[mapping["primary"], "secid"].unique().tolist()
    secids = list(secids)
    for i, s in enumerate(secids, 1):
        try:
            ps.update(s, iss, date_from, date_till)
        except Exception as e:  # noqa: BLE001
            log.warning("цены %s: %s", s, e)
        if i % 25 == 0:
            log.info("цены загружены: %d/%d", i, len(secids))
    ps.update_index(settings.market_index, iss, date_from, date_till)
    return ps.load_panel(secids)


# ----------------------------------------------------------------------------- исследование
def run_study(settings: Settings, events: pd.DataFrame, prices: pd.DataFrame, index: pd.DataFrame,
              out_dir: Optional[Path] = None, model: str = "market_model", bootstrap_iter: int = 2000) -> dict:
    """Event study + скрининг категорий + графики + markdown-отчёт. Возвращает пути к артефактам и таблицы."""
    from .analysis.event_study import EventStudyConfig, average_ar_path, compute_car_table, screen_categories, summarize
    from .analysis.report import plot_car_paths, write_markdown_report

    out_dir = Path(out_dir or settings.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = EventStudyConfig(model=model, bootstrap_iter=bootstrap_iter)
    res = compute_car_table(events, prices, index, cfg)
    car_table, diagnostics = (res if isinstance(res, tuple) else (res.table, res.diagnostics))
    summary = summarize(car_table)
    screening = screen_categories(summary)
    paths = average_ar_path(events, prices, index, cfg)
    car_table.to_parquet(out_dir / "car_table.parquet", index=False)
    summary.to_csv(out_dir / "summary.csv", index=False)
    screening.to_csv(out_dir / "screening.csv", index=False)
    paths.to_csv(out_dir / "car_paths.csv", index=False)
    png = plot_car_paths(paths, out_dir / "car_paths.png")
    sections = [
        ("Диагностика", pd.DataFrame([diagnostics]) if isinstance(diagnostics, dict) else str(diagnostics)),
        ("Скрининг категорий (окно 0..20, BH-FDR)", screening),
        ("Сводка по категориям, направлениям и окнам", summary),
    ]
    md = out_dir / "event_study.md"
    write_markdown_report(md, sections)
    return {"car_table": car_table, "summary": summary, "screening": screening, "paths": paths,
            "report": md, "plot": png, "diagnostics": diagnostics}


def run_backtest(settings: Settings, events: pd.DataFrame, prices: pd.DataFrame, index: pd.DataFrame,
                 out_dir: Optional[Path] = None, hold_days: int = 20, categories: Optional[Iterable[str]] = None,
                 direction: int = 1, split_date: Optional[str] = None, placebo_iter: int = 200) -> dict:
    from .analysis.backtest import BacktestConfig, random_event_benchmark, run_backtest as _run, walk_forward
    from .analysis.report import plot_equity, write_markdown_report

    out_dir = Path(out_dir or settings.reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = BacktestConfig(hold_days=hold_days, direction_filter=direction, categories=list(categories) if categories else None)
    result = _run(events, prices, index, cfg)
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.daily.to_csv(out_dir / "equity.csv", index=False)
    png = plot_equity(result.daily, out_dir / "equity.png")
    sections = [("Метрики стратегии", pd.DataFrame([result.metrics]).T.reset_index().rename(columns={"index": "metric", 0: "value"}))]
    placebo = None
    if placebo_iter:
        placebo = random_event_benchmark(events, prices, index, cfg, n_iter=placebo_iter)
        sections.append(("Плацебо (случайные даты)", pd.DataFrame([placebo]) if isinstance(placebo, dict) else placebo))
    wf = None
    if split_date:
        wf = walk_forward(events, prices, index, {"hold_days": [10, 20, 40, 60]}, split_date)
        sections.append(("Walk-forward", pd.DataFrame([{"in_sample": str(wf.get("in_sample")), "out_of_sample": str(wf.get("out_of_sample")),
                                                        "best_params": str(wf.get("best_params"))}])))
    md = out_dir / "backtest.md"
    write_markdown_report(md, sections)
    return {"result": result, "placebo": placebo, "walk_forward": wf, "report": md, "plot": png}
