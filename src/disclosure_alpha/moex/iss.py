"""Клиент MOEX ISS (Informational & Statistical Server).

Справочник API: https://iss.moex.com/iss/reference/ . Все запросы -- GET JSON через общий
``HttpClient`` (дисковый кеш, троттлинг, повторы). Формат ответа ISS:
``{block: {"columns": [...], "data": [[...], ...]}}``; большие выборки отдаются постранично
(параметр ``start``; у history есть курсор ``history.cursor`` с INDEX/TOTAL/PAGESIZE).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Iterable, Optional, Union

import numpy as np
import pandas as pd

from disclosure_alpha.config import Settings
from disclosure_alpha.http import HttpClient
from disclosure_alpha.models import PRICE_COLUMNS

log = logging.getLogger(__name__)

DateLike = Union[str, dt.date, dt.datetime, pd.Timestamp]

# Размер страницы ISS: securities.json допускает limit 5/10/20/100, history -- всегда 100 строк.
SECURITIES_PAGE_LIMIT = 100
HISTORY_PAGE_LIMIT = 100
MAX_PAGES = 5000  # защита от бесконечной пагинации
# TTL дискового кеша клиента по умолчанию (список бумаг меняется; история прошлых дат -- нет).
ISS_CACHE_TTL_SEC = 24 * 3600

SECURITIES_COLUMNS = [
    "secid", "shortname", "name", "isin", "is_traded", "emitent_id", "emitent_title",
    "emitent_inn", "emitent_okpo", "type", "group", "primary_boardid",
]
HISTORY_COLUMNS = "TRADEDATE,SECID,OPEN,HIGH,LOW,CLOSE,VOLUME,VALUE,NUMTRADES"
INDEX_HISTORY_COLUMNS = "TRADEDATE,SECID,CLOSE,OPEN,HIGH,LOW,VALUE"
# Схема prices из models.py + numtrades (число сделок; пригодится для фильтра ликвидности).
PRICE_OUTPUT_COLUMNS = PRICE_COLUMNS + ["numtrades"]
_PRICE_DTYPES = {
    "date": "object", "secid": "object", "open": "float64", "high": "float64", "low": "float64",
    "close": "float64", "volume": "int64", "value": "float64", "numtrades": "int64",
}


# ----------------------------------------------------------------------------- утилиты
def to_iso_date(d: DateLike) -> str:
    """Дата любого вида -> 'YYYY-MM-DD' (формат дат ISS)."""
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def to_date(d: DateLike) -> dt.date:
    """Дата любого вида -> datetime.date."""
    return pd.Timestamp(d).date()


def iss_table_to_df(payload: dict, block: str) -> pd.DataFrame:
    """Блок ответа ISS ``{block: {"columns": [...], "data": [...]}}`` -> DataFrame.

    Блока нет -> пустой DataFrame без колонок; блок без строк -> пустой DataFrame с колонками.
    """
    table = (payload or {}).get(block)
    if not isinstance(table, dict):
        return pd.DataFrame()
    columns = list(table.get("columns") or [])
    data = table.get("data") or []
    return pd.DataFrame(data, columns=columns)


def read_cursor(payload: dict, block: str = "history.cursor") -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Курсор пагинации ISS -> (INDEX, TOTAL, PAGESIZE); (None, None, None), если курсора нет."""
    cur = iss_table_to_df(payload, block)
    if cur.empty or not {"INDEX", "TOTAL", "PAGESIZE"} <= set(cur.columns):
        return None, None, None
    row = cur.iloc[0]
    try:
        return int(row["INDEX"]), int(row["TOTAL"]), int(row["PAGESIZE"])
    except (TypeError, ValueError):
        return None, None, None


def empty_prices() -> pd.DataFrame:
    """Пустая таблица схемы prices с правильными типами."""
    return pd.DataFrame({c: pd.Series([], dtype=_PRICE_DTYPES[c]) for c in PRICE_OUTPUT_COLUMNS})


def empty_index() -> pd.DataFrame:
    """Пустая таблица схемы index."""
    return pd.DataFrame({"date": pd.Series([], dtype="object"), "close": pd.Series([], dtype="float64")})


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """Колонка как float64 (сплошной NaN, если колонки нет)."""
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").astype("float64")
    return pd.Series(np.nan, index=df.index, dtype="float64")


def history_to_prices(raw: pd.DataFrame, secid: Optional[str] = None) -> pd.DataFrame:
    """Таблица ``history`` ISS (TRADEDATE, SECID, OPEN, ...) -> схема ``prices``.

    Строки без CLOSE (дни без сделок) отбрасываются; volume/numtrades -> int64; date -> datetime.date.
    """
    if raw.empty or "TRADEDATE" not in raw.columns:
        return empty_prices()
    dates = pd.to_datetime(raw["TRADEDATE"], errors="coerce")
    out = pd.DataFrame({
        "date": dates.dt.date,
        "secid": raw["SECID"].astype(str) if "SECID" in raw.columns else secid,
        "open": _num(raw, "OPEN"),
        "high": _num(raw, "HIGH"),
        "low": _num(raw, "LOW"),
        "close": _num(raw, "CLOSE"),
        "volume": _num(raw, "VOLUME"),
        "value": _num(raw, "VALUE"),
        "numtrades": _num(raw, "NUMTRADES"),
    })
    out = out[out["close"].notna() & dates.notna()].copy()
    for c in ("volume", "numtrades"):
        out[c] = out[c].fillna(0).round().astype("int64")
    out = out.sort_values("date", kind="stable").drop_duplicates("date", keep="last").reset_index(drop=True)
    return out[PRICE_OUTPUT_COLUMNS]


def index_history_to_df(raw: pd.DataFrame) -> pd.DataFrame:
    """Таблица ``history`` индекса -> схема ``index`` (date, close + open/high/low/value, если есть)."""
    if raw.empty or "TRADEDATE" not in raw.columns:
        return empty_index()
    dates = pd.to_datetime(raw["TRADEDATE"], errors="coerce")
    out = pd.DataFrame({"date": dates.dt.date, "close": _num(raw, "CLOSE")})
    for c in ("open", "high", "low", "value"):
        if c.upper() in raw.columns:
            out[c] = _num(raw, c.upper())
    out = out[out["close"].notna() & dates.notna()]
    return out.sort_values("date", kind="stable").drop_duplicates("date", keep="last").reset_index(drop=True)


def merge_securities(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Объединить списки бумаг; при дублях secid оставить строку с is_traded=1 (иначе первую)."""
    parts = [f for f in frames if f is not None and not f.empty]
    if not parts:
        return pd.DataFrame(columns=SECURITIES_COLUMNS)
    df = pd.concat(parts, ignore_index=True)
    df["is_traded"] = pd.to_numeric(df["is_traded"], errors="coerce").fillna(0).astype("int64")
    df = df.sort_values(["secid", "is_traded"], ascending=[True, False], kind="stable")
    return df.drop_duplicates("secid", keep="first").reset_index(drop=True)


def primary_board(description: dict) -> Optional[str]:
    """Код основного режима торгов (is_primary=1) из результата get_security_description."""
    for b in description.get("boards") or []:
        if str(b.get("is_primary")) == "1":
            return b.get("boardid")
    return None


def _clean_securities(df: pd.DataFrame) -> pd.DataFrame:
    """Гарантировать набор колонок SECURITIES_COLUMNS и типы (is_traded int, emitent_id Int64, ИНН str)."""
    df = df.copy()
    for c in SECURITIES_COLUMNS:
        if c not in df.columns:
            df[c] = None
    df["is_traded"] = pd.to_numeric(df["is_traded"], errors="coerce").fillna(0).astype("int64")
    df["emitent_id"] = pd.to_numeric(df["emitent_id"], errors="coerce").astype("Int64")
    for c in ("emitent_inn", "emitent_okpo"):
        df[c] = df[c].map(lambda v: None if pd.isna(v) else str(v).strip() or None)
    ordered = SECURITIES_COLUMNS + [c for c in df.columns if c not in SECURITIES_COLUMNS]
    return df[ordered].reset_index(drop=True)


def _is_final(till: dt.date) -> bool:
    """Итоги торгов за прошедшие дни не меняются -> такие ответы можно кешировать."""
    return till < dt.date.today()


# ----------------------------------------------------------------------------- клиент
class IssClient:
    """Обёртка над ISS: список бумаг, дневная история бумаги и индекса, паспорт бумаги.

    ``page_limit`` -- размер страницы для securities.json (ISS допускает 5/10/20/100).
    Без ``http`` создаётся HttpClient с параметрами из Settings и кешем ``{cache_dir}/iss`` (TTL сутки).
    """

    def __init__(self, settings: Settings, http: Optional[HttpClient] = None, page_limit: int = SECURITIES_PAGE_LIMIT):
        self.settings = settings
        self.base_url = settings.iss_base_url.rstrip("/")
        self.page_limit = int(page_limit)
        self.http = http or HttpClient(
            min_interval_sec=settings.iss_min_interval_sec,
            user_agent=settings.user_agent,
            timeout_sec=settings.timeout_sec,
            max_retries=settings.max_retries,
            cache_dir=settings.cache_dir / "iss",
            cache_ttl_sec=ISS_CACHE_TTL_SEC,
        )

    # ------------------------------------------------------------- низкий уровень
    def _get(self, url: str, params: dict, use_cache: bool = True) -> dict:
        """GET JSON; ISS всегда отвечает словарём блоков."""
        payload = self.http.get_json(url, params=params, use_cache=use_cache)
        if not isinstance(payload, dict):
            raise ValueError(f"unexpected ISS payload for {url}: {type(payload).__name__}")
        return payload

    def _get_history_pages(self, url: str, params: dict, use_cache: bool) -> pd.DataFrame:
        """Собрать все страницы блока ``history`` по курсору history.cursor.

        Запасной вариант без курсора: страница короче HISTORY_PAGE_LIMIT -> последняя.
        """
        frames: list[pd.DataFrame] = []
        start = 0
        for _ in range(MAX_PAGES):
            payload = self._get(url, {**params, "start": start}, use_cache=use_cache)
            page = iss_table_to_df(payload, "history")
            n = len(page)
            if n == 0:
                break
            frames.append(page)
            _index, total, pagesize = read_cursor(payload)
            if total is not None and pagesize:
                start += pagesize
                if start >= total:
                    break
            else:
                if n < HISTORY_PAGE_LIMIT:
                    break
                start += n
        else:
            raise RuntimeError(f"ISS pagination did not finish after {MAX_PAGES} pages: {url}")
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    # ------------------------------------------------------------- список бумаг
    def list_securities(
        self,
        engine: str = "stock",
        market: str = "shares",
        is_trading: Optional[int] = None,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """Список бумаг ``GET /securities.json`` (постранично по ``start``).

        ``is_trading``: 1 -- торгуемые, 0 -- неторгуемые (делистинг), None -- без фильтра.
        Стоп, когда страница короче ``page_limit`` (в т.ч. пустая).
        """
        url = f"{self.base_url}/securities.json"
        frames: list[pd.DataFrame] = []
        start = 0
        pages = 0
        while pages < MAX_PAGES:
            params: dict[str, Any] = {"engine": engine, "market": market}
            if is_trading is not None:
                params["is_trading"] = int(is_trading)
            params.update({"start": start, "limit": self.page_limit, "iss.meta": "off"})
            page = iss_table_to_df(self._get(url, params, use_cache=use_cache), "securities")
            pages += 1
            n = len(page)
            if n:
                frames.append(page)
            if n < self.page_limit:
                break
            start += n
        else:
            raise RuntimeError(f"ISS pagination did not finish after {MAX_PAGES} pages: {url}")
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SECURITIES_COLUMNS)
        df = _clean_securities(df)
        log.info("list_securities(%s/%s, is_trading=%s): %d rows in %d pages", engine, market, is_trading, len(df), pages)
        return df

    def list_all_shares(self, engine: str = "stock", market: str = "shares") -> pd.DataFrame:
        """Торгуемые + неторгуемые (делистинг) бумаги рынка -- список без survivorship bias.

        Дубли secid схлопываются, приоритет у строки с is_traded=1.
        """
        traded = self.list_securities(engine, market, is_trading=1)
        delisted = self.list_securities(engine, market, is_trading=0)
        df = merge_securities([traded, delisted])
        log.info("list_all_shares(%s/%s): %d traded + %d delisted -> %d unique", engine, market, len(traded), len(delisted), len(df))
        return df

    # ------------------------------------------------------------- история
    def get_history(
        self,
        secid: str,
        date_from: DateLike,
        date_till: Optional[DateLike] = None,
        board: Optional[str] = None,
    ) -> pd.DataFrame:
        """Дневные свечи бумаги в режиме ``board`` (по умолчанию settings.iss_board) за [date_from, date_till].

        ``date_till=None`` -> сегодня. Дни без сделок (CLOSE = null) отбрасываются. Ответы с ``till``
        не раньше сегодняшнего дня не кешируются (данные ещё могут измениться).
        """
        board = board or self.settings.iss_board
        till = to_date(date_till) if date_till is not None else dt.date.today()
        url = f"{self.base_url}/history/engines/stock/markets/shares/boards/{board}/securities/{secid}.json"
        params = {
            "from": to_iso_date(date_from),
            "till": to_iso_date(till),
            "iss.meta": "off",
            "iss.only": "history,history.cursor",
            "history.columns": HISTORY_COLUMNS,
        }
        raw = self._get_history_pages(url, params, use_cache=_is_final(till))
        df = history_to_prices(raw, secid)
        log.info("get_history(%s, %s, %s..%s): %d rows", secid, board, params["from"], params["till"], len(df))
        return df

    def get_index_history(
        self,
        index_secid: Optional[str] = None,
        date_from: Optional[DateLike] = None,
        date_till: Optional[DateLike] = None,
    ) -> pd.DataFrame:
        """История индекса (режим SNDX) -> схема ``index``: date, close (+ open, high, low, value).

        ``index_secid=None`` -> settings.market_index; ``date_from`` обязателен; ``date_till=None`` -> сегодня.
        """
        if date_from is None:
            raise ValueError("date_from is required")
        index_secid = index_secid or self.settings.market_index
        till = to_date(date_till) if date_till is not None else dt.date.today()
        url = f"{self.base_url}/history/engines/stock/markets/index/boards/SNDX/securities/{index_secid}.json"
        params = {
            "from": to_iso_date(date_from),
            "till": to_iso_date(till),
            "iss.meta": "off",
            "iss.only": "history,history.cursor",
            "history.columns": INDEX_HISTORY_COLUMNS,
        }
        raw = self._get_history_pages(url, params, use_cache=_is_final(till))
        df = index_history_to_df(raw)
        log.info("get_index_history(%s, %s..%s): %d rows", index_secid, params["from"], params["till"], len(df))
        return df

    # ------------------------------------------------------------- паспорт бумаги
    def get_security_description(self, secid: str) -> dict:
        """Паспорт бумаги ``GET /securities/{secid}.json``.

        Возвращает {name: value} блока description (SECID, ISIN, TYPE, ISSUESIZE, ...; значения -- строки,
        как отдаёт ISS) плюс ключ ``boards`` -- список словарей блока boards (boardid, is_primary,
        is_traded, history_from, history_till, ...). См. также primary_board().
        """
        url = f"{self.base_url}/securities/{secid}.json"
        payload = self._get(url, {"iss.meta": "off", "iss.only": "description,boards"})
        desc = iss_table_to_df(payload, "description")
        result: dict[str, Any] = {}
        if not desc.empty and {"name", "value"} <= set(desc.columns):
            result = dict(zip(desc["name"], desc["value"]))
        boards = iss_table_to_df(payload, "boards")
        result["boards"] = (
            boards.astype(object).where(boards.notna(), None).to_dict("records") if not boards.empty else []
        )
        return result
