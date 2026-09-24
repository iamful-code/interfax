"""CLI: disclosure-alpha <команда> [опции]. Порядок: discover → companies → messages → events → prices → study → backtest.

`demo` прогоняет исследование и бэктест на синтетических данных (без сети) -- для проверки конвейера и отчётов.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .config import load_settings


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _ed_client(args, settings):
    """Клиент e-disclosure с учётом флагов --browser / --show-browser (или переменной DA_BROWSER)."""
    from .edisclosure.client import EDisclosureClient

    use_browser = bool(getattr(args, "browser", False) or getattr(args, "show_browser", False)) or None
    if getattr(args, "show_browser", False):
        settings.browser_headless = False
        # в видимом окне капчу проходит человек -- ждём дольше
        settings.browser_warmup_timeout_sec = max(settings.browser_warmup_timeout_sec, 300.0)
    return EDisclosureClient(settings, use_browser=use_browser)


def _categories(s: str | None) -> list[str] | None:
    return [c.strip() for c in s.split(",") if c.strip()] if s else None


def cmd_discover(args):
    settings = load_settings()
    client = _ed_client(args, settings)
    try:
        summary = client.discover(Path(args.out) if args.out else None)
    finally:
        client.close()
    print("Сохранено:", settings.discovery_dir if not args.out else args.out)
    print(f"  режим: {summary.get('transport')}; User-Agent: {summary.get('user_agent')}; "
          f"cookies: {summary.get('cookies_loaded', 0)}")
    for name, page in summary["pages"].items():
        line = f"  {name}: {page.get('status')}"
        if page.get("error_file"):
            line += f" -> страница ошибки: {page['error_file']}"
        print(line)
        if page.get("headers"):
            print(f"      заголовки: {page['headers']}")
        dom = page.get("dom")
        if dom:
            c = dom["counts"]
            print(f"      заголовок: {dom['title'][:70]!r}")
            print(f"      структура: форм={c['forms']} полей={c['inputs']} селектов={c['selects']} "
                  f"ссылок={c['anchors']} таблиц={c['tables']} строк={c['rows']} скриптов={c['scripts']} "
                  f"iframe={c['iframes']} размер={c['html_len']}")
            if dom["forms"]:
                for f in dom["forms"][:3]:
                    names = [x["name"] for x in f["fields"] if x["name"]]
                    print(f"      форма: action={f['action']} method={f['method']} полей={f['n_fields']} "
                          f"имена={names[:12]}")
            elif dom["inputs_outside_forms"]:
                print(f"      поля вне форм: {[x.get('name') or x.get('id') or x.get('placeholder') for x in dom['inputs_outside_forms'][:12]]}")
            if dom.get("text_fields"):
                print(f"      текстовые поля: {[(x['name'] or x['id'], x['type']) for x in dom['text_fields'][:12]]}")
            if dom.get("buttons"):
                print(f"      кнопки: {[(b.get('id') or b.get('text')) for b in dom['buttons'][:8]]}")
            if dom.get("anchor_patterns"):
                print(f"      виды ссылок: {dom['anchor_patterns'][:12]}")
            if dom["script_srcs"]:
                print(f"      скрипты: {dom['script_srcs'][:5]}")
            if dom["event_links"]:
                print(f"      ссылки на события: {dom['event_links'][:3]}")
        alt = page.get("alternate_host")
        if alt:
            print(f"      альтернативный хост {alt.get('base_url')}: {alt.get('status')}")
    for h in summary.get("hints", []):
        print(f"  ! {h}")
    form = summary["pages"].get("search", {}).get("form")
    if form:
        print(f"  форма: method={form['method']} action={form['action']} полей={len(form['fields'])} "
              f"чекбокс-групп={list(form['checkbox_groups'])} типов сообщений={len(form['event_type_options'])}")


def cmd_companies(args):
    from .pipeline import build_universe
    settings = load_settings()
    client = _ed_client(args, settings)
    try:
        df = build_universe(settings, client, limit=args.limit, resolve_edisclosure_ids=not args.no_resolve)
    finally:
        client.close()
    print(f"компаний с id e-disclosure: {len(df)}")


def cmd_messages(args):
    from .pipeline import collect_messages, ensure_company_info
    settings = load_settings()
    ed = _ed_client(args, settings)
    company_ids = None
    if args.company_ids:
        company_ids = [int(x) for x in args.company_ids.split(",")]
    elif args.mapped_companies:
        comp = pd.read_parquet(settings.processed_dir / "companies.parquet")
        company_ids = comp["company_id"].dropna().astype(int).tolist()
    try:
        df = collect_messages(settings, ed, _date(args.date_from), _date(args.date_till), _categories(args.categories),
                              company_ids, chunk_days=args.chunk_days)
        if args.fetch_company_info:
            ensure_company_info(settings, ed, df["company_id"].dropna().astype(int).unique())
    finally:
        ed.close()
    print(f"сообщений в хранилище: {len(df)}")


def cmd_events(args):
    from .pipeline import build_events_table, ensure_company_info, fetch_events
    settings = load_settings()
    ed = _ed_client(args, settings)
    cats = _categories(args.categories) or ["insider_stake_change"]
    msgs = pd.read_parquet(settings.processed_dir / "messages.parquet")
    try:
        ensure_company_info(settings, ed, msgs["company_id"].dropna().astype(int).unique())
        trades = fetch_events(settings, ed, cats, limit=args.limit, only_mapped=not args.all_companies)
    finally:
        ed.close()
    print(f"событий разобрано: {len(trades)}")
    if len(trades) and "direction" in trades.columns:
        print(trades["direction"].value_counts(dropna=False).to_string())
    ev = build_events_table(settings)
    print(f"таблица событий: {len(ev)} строк")


def cmd_prices(args):
    from .pipeline import update_prices
    settings = load_settings()
    secids = args.secids.split(",") if args.secids else None
    panel = update_prices(settings, _date(args.date_from), _date(args.date_till), secids)
    print(f"строк цен: {len(panel)}, бумаг: {panel['secid'].nunique() if len(panel) else 0}")


def _load_inputs(settings, categories):
    from .moex.prices import PriceStore
    from .pipeline import build_events_table
    events = build_events_table(settings, categories)
    ps = PriceStore(settings)
    prices = ps.load_panel(events["secid"].unique().tolist())
    index = ps.load_index(settings.market_index)
    if index is None or not len(index):
        raise SystemExit("нет индекса -- выполните `disclosure-alpha prices`")
    return events, prices, index


def cmd_study(args):
    from .pipeline import run_study
    settings = load_settings()
    events, prices, index = _load_inputs(settings, _categories(args.categories))
    res = run_study(settings, events, prices, index, args.out, model=args.model, bootstrap_iter=args.bootstrap)
    print("отчёт:", res["report"])
    print(res["screening"].head(20).to_string())


def cmd_backtest(args):
    from .pipeline import run_backtest
    settings = load_settings()
    events, prices, index = _load_inputs(settings, None)
    res = run_backtest(settings, events, prices, index, args.out, hold_days=args.hold_days,
                       categories=_categories(args.categories) or ["insider_stake_change"], direction=args.direction,
                       split_date=args.split_date, placebo_iter=args.placebo)
    print("отчёт:", res["report"])
    for k, v in res["result"].metrics.items():
        print(f"  {k}: {v}")


def cmd_demo(args):
    from .pipeline import run_backtest, run_study
    from .synthetic import make_synthetic
    settings = load_settings()
    out = Path(args.out) if args.out else settings.reports_dir / "demo"
    events, prices, index = make_synthetic(seed=args.seed)
    res = run_study(settings, events, prices, index, out, model="market_model", bootstrap_iter=300)
    print("event study:", res["report"])
    print(res["screening"].to_string())
    bt = run_backtest(settings, events, prices, index, out, hold_days=10, categories=["insider_stake_change"],
                      direction=1, split_date=str(events["published_at"].median().date()), placebo_iter=50)
    print("backtest:", bt["report"])
    for k in ("total_return", "sharpe", "max_drawdown", "n_trades", "hit_rate"):
        print(f"  {k}: {bt['result'].metrics.get(k)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="disclosure-alpha", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_browser_flag(sp):
        sp.add_argument("--browser", action="store_true",
                        help="ходить через настоящий Chromium (Playwright): сам проходит JS-проверку сайта")
        sp.add_argument("--show-browser", action="store_true",
                        help="то же, но с видимым окном браузера (если требуется пройти капчу вручную)")
        return sp

    s = add_browser_flag(sub.add_parser("discover", help="скачать ключевые страницы e-disclosure и описать их структуру"))
    s.add_argument("--out")
    s.set_defaults(func=cmd_discover)

    s = add_browser_flag(sub.add_parser("companies", help="справочник акций MOEX (ISS) + сопоставление с компаниями e-disclosure по ИНН"))
    s.add_argument("--limit", type=int)
    s.add_argument("--no-resolve", action="store_true", help="только ISS, без поиска id на e-disclosure")
    s.set_defaults(func=cmd_companies)

    s = add_browser_flag(sub.add_parser("messages", help="собрать строки поиска сообщений за период"))
    s.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD")
    s.add_argument("--till", dest="date_till", required=True, help="YYYY-MM-DD")
    s.add_argument("--categories", help="через запятую, напр. insider_stake_change,buyback (см. config/event_types.yaml)")
    s.add_argument("--company-ids", help="id компаний e-disclosure через запятую (поиск по компаниям)")
    s.add_argument("--mapped-companies", action="store_true", help="искать по всем компаниям из companies.parquet")
    s.add_argument("--chunk-days", type=int, default=1)
    s.add_argument("--fetch-company-info", action="store_true", help="сразу загрузить ИНН/ОГРН новых компаний")
    s.set_defaults(func=cmd_messages)

    s = add_browser_flag(sub.add_parser("events", help="загрузить тексты сообщений и разобрать сделки инсайдеров; собрать таблицу событий"))
    s.add_argument("--categories", default="insider_stake_change")
    s.add_argument("--limit", type=int)
    s.add_argument("--all-companies", action="store_true", help="не ограничиваться эмитентами, сопоставленными с MOEX")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("prices", help="загрузить дневные свечи MOEX ISS и индекс")
    s.add_argument("--from", dest="date_from", required=True)
    s.add_argument("--till", dest="date_till", required=True)
    s.add_argument("--secids")
    s.set_defaults(func=cmd_prices)

    s = sub.add_parser("study", help="event study и скрининг категорий")
    s.add_argument("--categories")
    s.add_argument("--model", default="market_model", choices=["market_model", "market_adjusted"])
    s.add_argument("--bootstrap", type=int, default=2000)
    s.add_argument("--out")
    s.set_defaults(func=cmd_study)

    s = sub.add_parser("backtest", help="бэктест стратегии по событиям")
    s.add_argument("--categories", default="insider_stake_change")
    s.add_argument("--hold-days", type=int, default=20)
    s.add_argument("--direction", type=int, default=1, choices=[1, -1, 0])
    s.add_argument("--split-date", help="YYYY-MM-DD для walk-forward")
    s.add_argument("--placebo", type=int, default=200)
    s.add_argument("--out")
    s.set_defaults(func=cmd_backtest)

    s = sub.add_parser("demo", help="сквозной прогон на синтетических данных (без сети)")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        args.func(args)
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "CaptchaRequired":
            print(f"\nСайт показал капчу: {exc}\n"
                  f"Команда: {' '.join(['disclosure-alpha', args.cmd, '--show-browser'])}", file=sys.stderr)
            return 2
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
