"""Тесты модуля MOEX (iss / prices / mapping) на фикстурах, без сети."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from disclosure_alpha.config import Settings
from disclosure_alpha.http import HttpClient, HttpError
from disclosure_alpha.models import PRICE_COLUMNS
from disclosure_alpha.moex.iss import (
    IssClient,
    history_to_prices,
    iss_table_to_df,
    merge_securities,
    primary_board,
    read_cursor,
)
from disclosure_alpha.moex.mapping import (
    build_inn_to_secids,
    fuzzy_name_candidates,
    map_companies_to_secids,
    name_tokens,
    normalize_inn,
    unmatched_companies,
)
from disclosure_alpha.moex.prices import PriceStore, liquidity_stats

FIXTURES = Path(__file__).parent / "fixtures"
CURSOR_COLUMNS = ["INDEX", "TOTAL", "PAGESIZE"]


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def d(day: int) -> dt.date:
    return dt.date(2024, 1, day)


# ----------------------------------------------------------------------------- фейковый транспорт
class FakeResponse:
    """Минимальный аналог requests.Response для HttpClient."""

    def __init__(self, url: str, payload, status: int = 200):
        self.url = url
        self.status_code = status
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")


class FakeIssSession:
    """Мини-ISS поверх фикстур: маршрутизация по URL, пагинация по start, фильтры is_trading и from/till.

    securities.json: строки page1 + page2 (при limit=4 страницы совпадают с файлами), фильтр по
    is_trading, срез [start, start+limit). history: строки фикстуры с TRADEDATE в [from, till],
    страницы по ``history_page_size`` с курсором history.cursor (или без него, если with_cursor=False).
    """

    def __init__(self, history_page_size: int = 2, with_cursor: bool = True):
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []
        self.history_page_size = history_page_size
        self.with_cursor = with_cursor
        p1, p2 = load_fixture("iss_securities_page1.json"), load_fixture("iss_securities_page2.json")
        self.sec_columns = list(p1["securities"]["columns"])
        self.sec_rows = p1["securities"]["data"] + p2["securities"]["data"]
        self.history = {"SBER": load_fixture("iss_history_SBER.json")}
        self.index = {"IMOEX": load_fixture("iss_index_IMOEX.json")}
        self.description = {"SBER": load_fixture("iss_description_SBER.json")}

    def request(self, method, url, params=None, data=None, headers=None, timeout=None, allow_redirects=True):
        params = dict(params or {})
        self.calls.append((url, params))
        payload = self._route(url, params)
        if payload is None:
            return FakeResponse(url, {"error": "not found"}, status=404)
        return FakeResponse(url, payload)

    @staticmethod
    def _secid(url: str) -> str:
        return url.rsplit("/", 1)[-1].removesuffix(".json")

    def _route(self, url: str, params: dict):
        if url.endswith("/securities.json"):
            return self._securities(params)
        if "/markets/shares/boards/" in url:
            return self._history(self.history.get(self._secid(url)), params)
        if "/markets/index/boards/" in url:
            return self._history(self.index.get(self._secid(url)), params)
        if "/securities/" in url:
            return self.description.get(self._secid(url))
        return None

    def _securities(self, params: dict) -> dict:
        rows = self.sec_rows
        if "is_trading" in params:
            i = self.sec_columns.index("is_traded")
            rows = [r for r in rows if r[i] == int(params["is_trading"])]
        start, limit = int(params.get("start", 0)), int(params.get("limit", 100))
        return {"securities": {"columns": self.sec_columns, "data": rows[start:start + limit]}}

    def _history(self, fixture, params: dict) -> dict:
        if fixture is None:  # ISS на неизвестный secid отвечает пустыми блоками, а не 404
            return {"history": {"columns": ["TRADEDATE"], "data": []},
                    "history.cursor": {"columns": CURSOR_COLUMNS, "data": [[0, 0, 100]]}}
        cols = fixture["history"]["columns"]
        i = cols.index("TRADEDATE")
        lo, hi = params.get("from", "0000-00-00"), params.get("till", "9999-12-31")
        rows = [r for r in fixture["history"]["data"] if lo <= r[i] <= hi]
        start, size = int(params.get("start", 0)), self.history_page_size
        payload = {"history": {"columns": cols, "data": rows[start:start + size]}}
        if self.with_cursor:
            payload["history.cursor"] = {"columns": CURSOR_COLUMNS, "data": [[start, len(rows), size]]}
        return payload


def make_client(tmp_path: Path | None = None, **fake_kw) -> tuple[IssClient, FakeIssSession]:
    session = FakeIssSession(**fake_kw)
    http = HttpClient(min_interval_sec=0, cache_dir=None, session=session)
    settings = Settings(data_dir=tmp_path) if tmp_path is not None else Settings()
    return IssClient(settings, http=http, page_limit=4), session


def history_calls(session: FakeIssSession) -> list[tuple[str, dict]]:
    return [(u, p) for u, p in session.calls if "/history/" in u]


@pytest.fixture
def securities() -> pd.DataFrame:
    client, _ = make_client()
    return client.list_all_shares()


# ----------------------------------------------------------------------------- iss: утилиты
def test_iss_table_to_df():
    payload = {"x": {"columns": ["a", "b"], "data": [[1, "q"], [2, None]]}}
    df = iss_table_to_df(payload, "x")
    assert list(df.columns) == ["a", "b"] and len(df) == 2
    assert iss_table_to_df(payload, "missing").empty
    assert iss_table_to_df({"x": {"columns": ["a"], "data": []}}, "x").columns.tolist() == ["a"]
    assert read_cursor({"history.cursor": {"columns": CURSOR_COLUMNS, "data": [[0, 250, 100]]}}) == (0, 250, 100)
    assert read_cursor({}) == (None, None, None)


def test_history_to_prices_empty_and_partial_columns():
    empty = history_to_prices(pd.DataFrame())
    assert empty.empty and list(empty.columns)[:8] == PRICE_COLUMNS
    raw = pd.DataFrame({"TRADEDATE": ["2024-01-04", "2024-01-03"], "CLOSE": [2.0, 1.0]})
    df = history_to_prices(raw, "XXX")
    assert df["date"].tolist() == [d(3), d(4)] and (df["secid"] == "XXX").all()
    assert df["open"].isna().all() and df["volume"].tolist() == [0, 0]


# ----------------------------------------------------------------------------- iss: список бумаг
def test_list_securities_pagination_stops_on_short_page():
    client, session = make_client()
    df = client.list_securities()
    assert [p["start"] for _, p in session.calls] == [0, 4]  # 2-я страница короче limit -> стоп
    assert len(df) == 7 and df["secid"].is_unique
    for col in ("secid", "shortname", "name", "isin", "is_traded", "emitent_id", "emitent_title",
                "emitent_inn", "type", "group", "primary_boardid"):
        assert col in df.columns
    for url, p in session.calls:
        assert url == "https://iss.moex.com/iss/securities.json"
        assert p["engine"] == "stock" and p["market"] == "shares" and p["limit"] == 4 and p["iss.meta"] == "off"
        assert "is_trading" not in p
    assert df["is_traded"].dtype == "int64"
    assert df.set_index("secid").loc["SBER", "emitent_inn"] == "7707083893"
    assert pd.isna(df.set_index("secid").loc["FIVE", "emitent_inn"])


def test_list_securities_stops_on_empty_page():
    client, session = make_client()
    df = client.list_securities(is_trading=1)  # ровно 4 торгуемых = полная страница -> ещё запрос -> 0 строк
    assert [p["start"] for _, p in session.calls] == [0, 4]
    assert all(p["is_trading"] == 1 for _, p in session.calls)
    assert len(df) == 4 and set(df["is_traded"]) == {1}


def test_list_securities_fills_missing_columns():
    client, session = make_client()
    i = session.sec_columns.index("isin")
    session.sec_columns = [c for c in session.sec_columns if c != "isin"]
    session.sec_rows = [r[:i] + r[i + 1:] for r in session.sec_rows]
    df = client.list_securities()
    assert "isin" in df.columns and df["isin"].isna().all() and len(df) == 7


def test_list_all_shares_includes_delisted_and_dedups():
    client, session = make_client()
    df = client.list_all_shares()
    assert {p["is_trading"] for _, p in session.calls} == {0, 1}
    assert df["secid"].is_unique and len(df) == 7
    by = df.set_index("secid")
    assert by.loc["SBER", "is_traded"] == 1 and by.loc["DSKY", "is_traded"] == 0


def test_merge_securities_prefers_traded_row():
    a = pd.DataFrame({"secid": ["SBER", "GAZP"], "is_traded": [1, 1], "shortname": ["Сбербанк", "ГАЗПРОМ ао"]})
    b = pd.DataFrame({"secid": ["SBER", "DSKY"], "is_traded": [0, 0], "shortname": ["Сбербанк (старый)", "ДетскийМир"]})
    m = merge_securities([b, a]).set_index("secid")  # порядок списков не важен
    assert sorted(m.index) == ["DSKY", "GAZP", "SBER"]
    assert m.loc["SBER", "is_traded"] == 1 and m.loc["SBER", "shortname"] == "Сбербанк"
    assert merge_securities([]).empty


# ----------------------------------------------------------------------------- iss: история
def test_get_history_cursor_pagination_and_types():
    client, session = make_client(history_page_size=2)
    df = client.get_history("SBER", "2024-01-01", dt.date(2024, 1, 10))
    calls = history_calls(session)
    assert [p["start"] for _, p in calls] == [0, 2, 4]  # TOTAL=6, PAGESIZE=2 -> 3 страницы
    url, p = calls[0]
    assert url == "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities/SBER.json"
    assert p["from"] == "2024-01-01" and p["till"] == "2024-01-10"
    assert p["iss.only"] == "history,history.cursor" and p["iss.meta"] == "off"
    assert p["history.columns"] == "TRADEDATE,SECID,OPEN,HIGH,LOW,CLOSE,VOLUME,VALUE,NUMTRADES"
    assert list(df.columns)[:8] == PRICE_COLUMNS
    assert df["date"].tolist() == [d(3), d(4), d(5), d(9), d(10)]  # 2024-01-08 без CLOSE отброшена
    assert all(isinstance(x, dt.date) for x in df["date"])
    assert df["close"].dtype == "float64" and df["value"].dtype == "float64"
    assert df["volume"].dtype == "int64" and df["numtrades"].dtype == "int64"
    assert df["close"].iloc[0] == pytest.approx(272.99) and df["volume"].iloc[0] == 42391650
    assert (df["secid"] == "SBER").all()


def test_get_history_without_cursor_falls_back_to_row_count():
    client, session = make_client(history_page_size=100, with_cursor=False)
    df = client.get_history("SBER", "2024-01-04", "2024-01-09")
    assert len(history_calls(session)) == 1  # страница короче 100 -> последняя
    assert df["date"].tolist() == [d(4), d(5), d(9)]


def test_get_history_unknown_secid_and_board():
    client, session = make_client()
    df = client.get_history("NOPE", "2024-01-01", "2024-01-10", board="SMAL")
    assert df.empty and list(df.columns)[:8] == PRICE_COLUMNS
    assert "/boards/SMAL/securities/NOPE.json" in session.calls[-1][0]


def test_get_index_history():
    client, session = make_client(history_page_size=100)
    df = client.get_index_history("IMOEX", "2024-01-01", "2024-01-10")
    assert session.calls[-1][0] == "https://iss.moex.com/iss/history/engines/stock/markets/index/boards/SNDX/securities/IMOEX.json"
    assert session.calls[-1][1]["history.columns"] == "TRADEDATE,SECID,CLOSE,OPEN,HIGH,LOW,VALUE"
    assert list(df.columns) == ["date", "close", "open", "high", "low", "value"]
    assert len(df) == 6 and df["close"].iloc[-1] == pytest.approx(3160.71)
    assert isinstance(df["date"].iloc[0], dt.date)
    client.get_index_history(date_from="2024-01-01", date_till="2024-01-10")  # индекс по умолчанию из Settings
    assert session.calls[-1][0].endswith("/IMOEX.json")
    with pytest.raises(ValueError):
        client.get_index_history("IMOEX")


def test_get_security_description():
    client, session = make_client()
    desc = client.get_security_description("SBER")
    assert desc["SECID"] == "SBER" and desc["ISIN"] == "RU0009029540" and desc["TYPE"] == "common_share"
    assert [b["boardid"] for b in desc["boards"]] == ["TQBR", "SMAL", "EQBR"]
    assert desc["boards"][0]["history_from"] == "2013-03-25"
    assert primary_board(desc) == "TQBR"
    p = session.calls[-1][1]
    assert p["iss.only"] == "description,boards" and p["iss.meta"] == "off"


def test_http_error_propagates():
    client, _ = make_client()
    with pytest.raises(HttpError):
        client.get_security_description("NOPE")  # фейк отвечает 404


# ----------------------------------------------------------------------------- prices
def test_price_store_incremental_update(tmp_path):
    client, session = make_client(tmp_path=tmp_path, history_page_size=100)
    store = PriceStore(client.settings)
    assert store.load("SBER") is None and store.list_secids() == []

    df1 = store.update("SBER", client, "2024-01-01", "2024-01-05")
    assert df1["date"].tolist() == [d(3), d(4), d(5)]
    assert store.path("SBER") == tmp_path / "prices" / "SBER.parquet" and store.path("SBER").exists()

    df2 = store.update("SBER", client, "2024-01-01", "2024-01-10")
    p = session.calls[-1][1]
    assert p["from"] == "2024-01-06" and p["till"] == "2024-01-10"  # запрошены только новые даты
    assert df2["date"].tolist() == [d(3), d(4), d(5), d(9), d(10)]

    loaded = store.load("SBER")
    assert loaded["date"].tolist() == df2["date"].tolist() and isinstance(loaded["date"].iloc[0], dt.date)
    assert loaded["volume"].dtype == "int64" and loaded["close"].iloc[-1] == pytest.approx(275.27)
    assert store.last_date("SBER") == d(10) and store.list_secids() == ["SBER"]

    n_calls = len(session.calls)
    df3 = store.update("SBER", client, "2024-01-01", "2024-01-10")  # уже актуально -> без запросов
    assert len(session.calls) == n_calls and len(df3) == 5

    df4 = store.update("NOPE", client, "2024-01-01", "2024-01-10")  # нет данных -> пусто, файл не создаётся
    assert df4.empty and not store.path("NOPE").exists()


def test_price_store_save_dedups_and_load_panel(tmp_path):
    store = PriceStore(Settings(data_dir=tmp_path))
    gazp = pd.DataFrame({
        "date": ["2024-01-04", "2024-01-03", "2024-01-04"], "secid": ["GAZP"] * 3,
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.5],
        "volume": [10.0, 20.0, 30.0], "value": [1.0, 2.0, 3.0],
    })
    store.save("GAZP", gazp)
    loaded = store.load("GAZP")
    assert loaded["date"].tolist() == [d(3), d(4)] and loaded["close"].tolist() == [2.0, 3.5]  # последняя запись даты выигрывает
    assert loaded["volume"].dtype == "int64" and "numtrades" in loaded.columns

    store.save("SBER", pd.DataFrame({"date": [d(3)], "close": [272.99]}))
    panel = store.load_panel(["SBER", "GAZP", "MISSING"])
    assert panel["secid"].tolist() == ["GAZP", "GAZP", "SBER"] and list(panel.columns)[:8] == PRICE_COLUMNS
    assert store.load_panel().equals(panel)  # без аргумента -- все сохранённые
    assert store.load_panel(["MISSING"]).empty


def test_price_store_index_update(tmp_path):
    client, session = make_client(tmp_path=tmp_path, history_page_size=100)
    store = PriceStore(client.settings)
    assert store.load_index("IMOEX") is None

    idx = store.update_index("IMOEX", client, "2024-01-01", "2024-01-05")
    assert idx["date"].tolist() == [d(3), d(4), d(5)] and store.index_path("IMOEX").exists()
    idx = store.update_index(None, client, "2024-01-01", "2024-01-10")  # None -> settings.market_index
    assert session.calls[-1][1]["from"] == "2024-01-06"
    assert len(idx) == 6 and list(idx.columns)[:2] == ["date", "close"]
    assert store.load_index()["close"].iloc[-1] == pytest.approx(3160.71)
    n_calls = len(session.calls)
    store.update_index("IMOEX", client, "2024-01-01", "2024-01-10")
    assert len(session.calls) == n_calls
    assert store.list_secids() == []  # индексы не считаются бумагами


def test_liquidity_stats():
    dates = [d(3), d(4), d(5), d(8), d(9), d(10)]
    liquid = pd.DataFrame({"date": dates, "secid": "LIQ", "volume": [100] * 6, "value": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0]})
    thin = pd.DataFrame({"date": [d(3), d(5), d(10)], "secid": "THIN", "volume": [100, 0, 50], "value": [1000.0, 0.0, 500.0]})
    old = pd.DataFrame({"date": [dt.date(2023, 6, 1)], "secid": "OLD", "volume": [7], "value": [70.0]})
    stats = liquidity_stats(pd.concat([liquid, thin, old]), window_days=10).set_index("secid")

    assert stats.loc["LIQ", "n_days"] == 6 and stats.loc["LIQ", "zero_volume_share"] == 0
    assert stats.loc["LIQ", "median_value"] == pytest.approx(35.0)
    # THIN: 6 календарных дней в окне, сделки в 2 -> доля дней без сделок 4/6; дни без сделок = 0 оборота
    assert stats.loc["THIN", "n_traded_days"] == 2 and stats.loc["THIN", "zero_volume_share"] == pytest.approx(4 / 6)
    assert stats.loc["THIN", "median_value"] == 0.0 and stats.loc["THIN", "mean_value"] == pytest.approx(250.0)
    # OLD: окно по собственной последней дате (делистинг), календарь панели содержит эту дату
    assert stats.loc["OLD", "n_days"] == 1 and stats.loc["OLD", "zero_volume_share"] == 0
    assert stats.loc["OLD", "last_date"] == dt.date(2023, 6, 1)

    common = liquidity_stats(pd.concat([liquid, old]), window_days=10, as_of=d(10)).set_index("secid")
    assert common.loc["OLD", "n_days"] == 6 and common.loc["OLD", "zero_volume_share"] == 1.0
    assert liquidity_stats(pd.DataFrame(columns=PRICE_COLUMNS)).empty


# ----------------------------------------------------------------------------- mapping
def test_normalize_inn_edge_cases():
    assert normalize_inn("7707083893") == "7707083893"
    assert normalize_inn(" 7707083893\n") == "7707083893"
    assert normalize_inn(7707083893) == "7707083893"
    assert normalize_inn(7707083893.0) == "7707083893"
    assert normalize_inn("7707083893.0") == "7707083893"
    assert normalize_inn("ИНН 7707-083893") == "7707083893"
    assert normalize_inn("770708389312") == "770708389312"
    assert normalize_inn("770708389") is None and normalize_inn("77070838931") is None
    for bad in (None, float("nan"), pd.NA, "", "n/a", "нет"):
        assert normalize_inn(bad) is None


def test_build_inn_to_secids_primary_selection(securities):
    m = build_inn_to_secids(securities)
    assert list(m.columns)[:8] == ["inn", "secid", "shortname", "is_traded", "type", "is_common", "is_preferred", "primary"]
    sber = m[m["inn"] == "7707083893"].set_index("secid")
    assert set(sber.index) == {"SBER", "SBERP"}
    assert bool(sber.loc["SBER", "primary"]) and not bool(sber.loc["SBERP", "primary"])
    assert bool(sber.loc["SBER", "is_common"]) and bool(sber.loc["SBERP", "is_preferred"])
    assert m.groupby("inn")["primary"].sum().eq(1).all()  # ровно одна primary на ИНН
    dsky = m[m["inn"] == "7729355029"]  # только делистингованная бумага -> всё равно сопоставляется
    assert len(dsky) == 1 and bool(dsky["primary"].iloc[0]) and dsky["is_traded"].iloc[0] == 0
    assert "FIVE" not in set(m["secid"])  # нет ИНН -> не в карте
    assert build_inn_to_secids(pd.DataFrame(columns=securities.columns)).empty


def test_primary_tie_breaks():
    df = pd.DataFrame({
        "secid": ["AAAA", "AAA", "AAAAP", "BBB", "BBBB", "ZZZ", "ZZ", "YYB", "YYA"],
        "emitent_inn": ["1" * 10] * 3 + ["2" * 10] * 2 + ["3" * 10] * 2 + ["4" * 10] * 2,
        "is_traded": [1, 0, 1, 1, 1, 1, 1, 1, 1],
        "type": ["common_share", "common_share", "preferred_share", "preferred_share", "depositary_receipt",
                 "common_share", "common_share", "common_share", "common_share"],
        "shortname": "x",
    })
    m = build_inn_to_secids(df).set_index("secid")["primary"]
    assert m["AAAA"] and not m["AAA"] and not m["AAAAP"]  # торгуемая обыкновенная важнее короткого secid
    assert m["BBB"] and not m["BBBB"]  # нет обыкновенных -> привилегированная
    assert m["ZZ"] and not m["ZZZ"]  # при равенстве -- самый короткий secid
    assert m["YYA"] and not m["YYB"]  # затем алфавит


def test_map_companies_to_secids_and_unmatched(securities, caplog):
    companies = pd.DataFrame({
        "company_id": [3043, 5, 7, 9],
        "name": ['ПАО "Сбербанк России"', 'ПАО "Детский мир"', 'ООО "Ромашка"', "Без ИНН"],
        "inn": [" 7707083893 ", 7729355029, "1234567890", None],
    })
    with caplog.at_level(logging.INFO, logger="disclosure_alpha.moex.mapping"):
        m = map_companies_to_secids(companies, securities)
    assert list(m.columns) == ["company_id", "inn", "secid", "shortname", "is_traded", "type", "primary"]
    assert set(m["company_id"]) == {3043, 5}
    assert m[m["company_id"] == 3043]["secid"].tolist() == ["SBER", "SBERP"]  # primary первой
    assert m[(m["company_id"] == 3043) & m["primary"]]["secid"].tolist() == ["SBER"]
    assert m[m["company_id"] == 5]["secid"].tolist() == ["DSKY"]
    assert "2/4 companies matched, 2 unmatched (1 without valid INN)" in caplog.text

    only = map_companies_to_secids(companies, securities, primary_only=True)
    assert len(only) == 2 and set(only["secid"]) == {"SBER", "DSKY"} and only["primary"].all()

    un = unmatched_companies(companies, securities)
    assert un.set_index("company_id")["reason"].to_dict() == {7: "no_match", 9: "no_inn"}
    assert list(un.columns) == ["company_id", "name", "inn", "reason"]


def test_fuzzy_name_candidates(securities):
    assert name_tokens('Публичное акционерное общество "Сбербанк России"') == {"сбербанк", "россии"}
    assert name_tokens(None) == set() and name_tokens(float("nan")) == set()

    c = fuzzy_name_candidates('ПАО "Сбербанк России"', securities)
    assert c.iloc[0]["secid"] == "SBER" and c.iloc[0]["score"] == pytest.approx(1.0)
    assert list(c.columns) == ["secid", "shortname", "emitent_title", "emitent_inn", "score"]
    assert "GAZP" not in set(c["secid"]) and len(c) <= 5

    c2 = fuzzy_name_candidates("Публичное акционерное общество «Нефтяная компания «ЛУКОЙЛ»", securities, top_n=1)
    assert c2["secid"].tolist() == ["LKOH"]
    assert fuzzy_name_candidates("", securities).empty
    assert fuzzy_name_candidates("Совершенно другое название", securities).empty
