from __future__ import annotations

import json
from datetime import date

from disclosure_alpha.config import Settings
from disclosure_alpha.edisclosure.client import EDisclosureClient
from disclosure_alpha.http import HttpClient
from tests.conftest import FakeSession, read_fixture


def _client(tmp_path, rules):
    settings = Settings(data_dir=tmp_path / "data", edisclosure_min_interval_sec=0.0)
    session = FakeSession(rules)
    http = HttpClient(min_interval_sec=0.0, session=session, cache_dir=None)
    return EDisclosureClient(settings, http=http), session


def _rules():
    def is_search_get(m, url, p, d):
        return m == "GET" and url.endswith("/poisk-po-soobshheniyam")

    def is_search_post(m, url, p, d):
        return m == "POST" and url.endswith("/poisk-po-soobshheniyam")

    def search_post(m, url, p, d):
        page = int(d.get("lastPageNumber", "1"))
        return (200, read_fixture("edisclosure_search_results.html") if page == 1 else read_fixture("edisclosure_search_results_empty.html"))

    return [
        (is_search_get, lambda *a: (200, read_fixture("edisclosure_search_page.html"))),
        (is_search_post, search_post),
        (lambda m, url, p, d: url.endswith("/portal/lastnews.aspx"), lambda *a: (200, read_fixture("edisclosure_lastnews.html"))),
        (lambda m, url, p, d: "event.aspx" in url and p.get("EventId") == "pHCdwvYkFkau-A8zAGePZqg-B-B",
         lambda *a: (200, read_fixture("edisclosure_event_stake_714.html"))),
        (lambda m, url, p, d: "event.aspx" in url, lambda *a: (200, read_fixture("edisclosure_event_stake_454_sale.html"))),
        (lambda m, url, p, d: "company.aspx" in url, lambda *a: (200, read_fixture("edisclosure_company.html"))),
    ]


def test_search_posts_form_defaults_and_overrides(tmp_path):
    client, session = _client(tmp_path, _rules())
    rows, _ = client.search_page(date(2024, 3, 14), date(2024, 3, 15), page=1, page_size=100, event_type_ids=["52", "12"])
    assert len(rows) == 3
    post = [c for c in session.calls if c["method"] == "POST"][0]
    d = post["data"]
    assert d["dateStart"] == "14.03.2024" and d["dateFinish"] == "15.03.2024"
    assert d["lastPageNumber"] == "1" and d["lastPageSize"] == "100"
    assert d["eventTypeCheckboxGroup"] == ["52", "12"]
    assert d["radView"] == "0"            # радио по умолчанию
    assert d["query"] == ""               # прочие поля формы переданы как есть
    assert post["url"].endswith("/poisk-po-soobshheniyam")


def test_iter_search_paginates_and_dedups(tmp_path):
    client, session = _client(tmp_path, _rules())
    rows = list(client.iter_search(date(2024, 3, 14), date(2024, 3, 15), chunk_days=2))
    assert [r.event_id for r in rows] == ["pHCdwvYkFkau-A8zAGePZqg-B-B", "9D-AeWqDylkCcWgqVVBEnbQ-B-B", "TZLsLVNQZU23CeRqQOP9uQ-B-B"]
    posts = [c for c in session.calls if c["method"] == "POST"]
    # страница 1 (3 строки < page_size) -> остановка без запроса страницы 2
    assert len(posts) == 1


def test_iter_search_stops_on_repeated_page(tmp_path):
    rules = _rules()
    # сайт игнорирует номер страницы и всегда отдаёт те же строки
    rules[1] = (rules[1][0], lambda *a: (200, read_fixture("edisclosure_search_results.html")))
    client, session = _client(tmp_path, rules)
    rows = list(client.iter_search(date(2024, 3, 14), date(2024, 3, 15), chunk_days=2, page_size=3))
    assert len(rows) == 3
    assert len([c for c in session.calls if c["method"] == "POST"]) == 2


def test_event_type_options_and_lastnews(tmp_path):
    client, _ = _client(tmp_path, _rules())
    opts = client.event_type_options()
    assert any(o["value"] == "52" for o in opts)
    news = client.lastnews()
    assert len(news) == 2


def test_get_event_and_company(tmp_path):
    client, _ = _client(tmp_path, _rules())
    ev = client.get_event("pHCdwvYkFkau-A8zAGePZqg-B-B")
    assert ev.company_id == 3043 and "до изменения" in ev.body_text
    ci = client.get_company(3043)
    assert ci.inn == "7707083893" and ci.company_id == 3043


def test_discover_writes_summary(tmp_path):
    client, _ = _client(tmp_path, _rules())
    out = tmp_path / "disc"
    summary = client.discover(out)
    assert (out / "search_page.html").exists() and (out / "lastnews.html").exists()
    assert (out / "event_sample.html").exists() and (out / "company_sample.html").exists()
    saved = json.loads((out / "discovery.json").read_text("utf-8"))
    assert saved["pages"]["search"]["form"]["method"] == "post"
    assert saved["pages"]["lastnews"]["rows_parsed"] == 2
    assert saved["pages"]["company"]["parsed"]["inn"] == "7707083893"
    assert summary["pages"]["event"]["parsed"]["company_id"] == 1976  # первое событие ленты -> фикстура 454


def test_field_map_override_from_config(tmp_path):
    cfg = tmp_path / "form.json"
    cfg.write_text(json.dumps({"field_map": {"date_from": "df", "date_till": "dt"}}), "utf-8")
    rules = _rules()
    client, session = _client(tmp_path, rules)
    client2 = EDisclosureClient(client.settings, http=client.http, form_config_path=cfg)
    client2.search_page(date(2024, 1, 1), date(2024, 1, 2))
    post = [c for c in session.calls if c["method"] == "POST"][-1]
    assert post["data"]["df"] == "01.01.2024" and post["data"]["dt"] == "02.01.2024"


def test_discover_captures_error_pages_and_tries_alternate_host(tmp_path):
    blocked = "<html><head><title>Интерфакс – Сервер раскрытия информации</title></head><body><script>document.cookie='__ddg1_=1'</script></body></html>"

    def search_get(m, url, p, d):
        return (200, read_fixture("edisclosure_search_page.html")) if "://www." in url else (503, blocked)

    rules = [
        (lambda m, url, p, d: m == "GET" and url.endswith("/poisk-po-soobshheniyam"), search_get),
        (lambda m, url, p, d: url.endswith("/portal/lastnews.aspx"), lambda *a: (503, blocked)),
    ]
    client, session = _client(tmp_path, rules)
    client.http.max_retries = 4
    out = tmp_path / "disc"
    summary = client.discover(out)
    assert client.http.max_retries == 4  # quick-режим восстанавливает настройку
    search = summary["pages"]["search"]
    assert search["status"] == "error: HTTP 503"
    assert (out / "search_page_error.html").exists() and "document.cookie" in (out / "search_page_error.html").read_text("utf-8")
    assert "JS-проверка" in search["diagnosis"] or "DDoS-Guard" in search["diagnosis"]
    assert search["alternate_host"]["status"] == "ok" and search["alternate_host"]["base_url"] == "https://www.e-disclosure.ru"
    assert "form" in search and search["form"]["method"] == "post"   # форма разобрана с альтернативного хоста
    assert any("DA_EDISCLOSURE_BASE_URL=https://www.e-disclosure.ru" in h for h in summary["hints"])
    assert summary["pages"]["lastnews"]["status"] == "error: HTTP 503"
    assert "event" not in summary["pages"]
    # в quick-режиме на 503 делается не больше одного повтора на URL
    calls_503 = [c for c in session.calls if c["url"].startswith("https://e-disclosure.ru/poisk")]
    assert len(calls_503) == 2
