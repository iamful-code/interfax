from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from disclosure_alpha.config import Settings
from disclosure_alpha.edisclosure.client import EDisclosureClient
from disclosure_alpha.http import HttpClient
from disclosure_alpha import pipeline
from tests.conftest import FakeSession, read_fixture

INN_BY_COMPANY = {3043: "7707083893", 1976: "0274051582", 379: "7707083899"}


def _rules():
    def company_page(m, url, p, d):
        cid = int(p.get("id"))
        html = read_fixture("edisclosure_company.html").replace("7707083893", INN_BY_COMPANY.get(cid, "0000000000"))
        return 200, html

    def search_post(m, url, p, d):
        page = int(d.get("lastPageNumber", "1"))
        return 200, read_fixture("edisclosure_search_results.html") if page == 1 else read_fixture("edisclosure_search_results_empty.html")

    return [
        (lambda m, url, p, d: m == "GET" and url.endswith("/poisk-po-soobshheniyam"), lambda *a: (200, read_fixture("edisclosure_search_page.html"))),
        (lambda m, url, p, d: m == "POST" and url.endswith("/poisk-po-soobshheniyam"), search_post),
        (lambda m, url, p, d: "event.aspx" in url and p.get("EventId") == "pHCdwvYkFkau-A8zAGePZqg-B-B",
         lambda *a: (200, read_fixture("edisclosure_event_stake_714.html"))),
        (lambda m, url, p, d: "event.aspx" in url, lambda *a: (200, read_fixture("edisclosure_event_stake_454_sale.html"))),
        (lambda m, url, p, d: "company.aspx" in url, company_page),
    ]


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", edisclosure_min_interval_sec=0.0)
    settings.ensure_dirs()
    session = FakeSession(_rules())
    ed = EDisclosureClient(settings, http=HttpClient(min_interval_sec=0.0, session=session, cache_dir=None))
    mapping = pd.DataFrame({"inn": ["7707083893", "7707083893", "0274051582"], "secid": ["SBER", "SBERP", "BANE"],
                            "shortname": ["Сбербанк", "Сбербанк-п", "Башнефт ао"], "is_traded": [1, 1, 1],
                            "type": ["common_share", "preferred_share", "common_share"], "primary": [True, False, True]})
    mapping.to_parquet(settings.processed_dir / "mapping.parquet", index=False)
    return settings, ed, session


def test_collect_messages_filters_by_category_via_form_ids(env):
    settings, ed, session = env
    df = pipeline.collect_messages(settings, ed, date(2024, 3, 14), date(2024, 3, 15), categories=["insider_stake_change"], chunk_days=2)
    post = [c for c in session.calls if c["method"] == "POST"][0]
    assert post["data"]["eventTypeCheckboxGroup"] == ["52"]   # id чекбокса с подписью про изменение доли
    assert len(df) == 1 and df.iloc[0]["company_id"] == 3043


def test_full_flow_messages_events_table(env):
    settings, ed, session = env
    msgs = pipeline.collect_messages(settings, ed, date(2024, 3, 14), date(2024, 3, 15), chunk_days=2)
    assert len(msgs) == 3
    companies = pipeline.ensure_company_info(settings, ed, msgs["company_id"].dropna().astype(int).unique())
    assert set(companies["inn"]) == {"7707083893", "0274051582", "7707083899"}
    # повторный вызов ничего не качает
    n_calls = len(session.calls)
    pipeline.ensure_company_info(settings, ed, [3043, 1976])
    assert len(session.calls) == n_calls

    trades = pipeline.fetch_events(settings, ed, ["insider_stake_change"])
    assert len(trades) == 1
    t = trades.iloc[0]
    assert t["direction"] == 1 and t["person"] == "Иванов Иван Иванович" and t["role"] == "management_board"
    assert (settings.raw_dir / "events").glob("*.json")

    ev = pipeline.build_events_table(settings)
    # 379 (дивиденды) не сопоставлена с MOEX -> отброшена; остаются инсайдер SBER и buyback BANE
    assert sorted(ev["secid"]) == ["BANE", "SBER"]
    ins = ev[ev["category"] == "insider_stake_change"].iloc[0]
    assert ins["secid"] == "SBER" and ins["direction"] == 1 and ins["size"] == pytest.approx(0.0013)
    bb = ev[ev["category"] == "buyback"].iloc[0]
    assert bb["direction"] == 1 and pd.isna(bb["size"])
    assert not ev["has_confounder"].any()
    assert (settings.processed_dir / "events.parquet").exists()


def test_flag_confounders():
    ev = pd.DataFrame({
        "event_id": ["a", "b", "c", "d"], "secid": ["X", "X", "X", "Y"],
        "published_at": pd.to_datetime(["2024-01-10", "2024-01-12", "2024-02-20", "2024-01-11"]),
        "category": ["insider_stake_change", "buyback", "insider_stake_change", "insider_stake_change"],
    })
    out = pipeline._flag_confounders(ev, window_days=5)
    assert out.set_index("event_id")["has_confounder"].to_dict() == {"a": True, "b": False, "c": False, "d": False}
