"""Клиент e-disclosure.ru: обнаружение структуры (discover), поиск сообщений, лента, страницы событий и компаний.

ВАЖНО. Живой доступ к сайту при разработке отсутствовал (сетевая политика + гео-блокировка:
сайт отвечает 403 не с российских IP). Имена полей формы поиска -- ПРЕДПОЛОЖЕНИЕ (см. FIELD_MAP),
их нужно подтвердить командой ``disclosure-alpha discover`` и при необходимости переопределить
в ``config/edisclosure_form.json`` (ключи как в FIELD_MAP). Разбор страниц опирается на устойчивые
признаки (ссылки event.aspx?EventId=, company.aspx?id=, даты), а не на точную вёрстку.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

from ..config import Settings, load_settings
from ..http import HttpClient, HttpError
from ..models import CompanyInfo, EventPage, MessageRow
from . import parsers

log = logging.getLogger(__name__)

SEARCH_PATH = "/poisk-po-soobshheniyam"
COMPANY_SEARCH_PATH = "/poisk-po-kompaniyam"
LASTNEWS_PATH = "/portal/lastnews.aspx"
EVENT_PATH = "/portal/event.aspx"
COMPANY_PATH = "/portal/company.aspx"

# логические поля -> предполагаемые имена полей формы поиска (проверить через discover!)
FIELD_MAP = {
    "date_from": "dateStart",
    "date_till": "dateFinish",
    "page": "lastPageNumber",
    "page_size": "lastPageSize",
    "event_types": "eventTypeCheckboxGroup",
    "query": "query",
    "query_id": "queryId",
    "event_type_term": "eventTypeTerm",
}
DEFAULT_PAGE_SIZE = 100
MAX_PAGES_PER_CHUNK = 500


def _fmt_date(d: date | datetime) -> str:
    return d.strftime("%d.%m.%Y")


class EDisclosureClient:
    def __init__(self, settings: Optional[Settings] = None, http: Optional[HttpClient] = None,
                 field_map: Optional[dict] = None, form_config_path: Optional[Path] = None):
        self.settings = settings or load_settings()
        self.base_url = self.settings.edisclosure_base_url.rstrip("/")
        self.http = http or HttpClient(
            min_interval_sec=self.settings.edisclosure_min_interval_sec,
            user_agent=self.settings.user_agent,
            timeout_sec=self.settings.timeout_sec,
            max_retries=self.settings.max_retries,
            cache_dir=self.settings.cache_dir / "edisclosure",
        )
        self.field_map = dict(FIELD_MAP)
        cfg = form_config_path or (self.settings.config_dir / "edisclosure_form.json")
        if cfg and Path(cfg).exists():
            try:
                self.field_map.update(json.loads(Path(cfg).read_text("utf-8")).get("field_map", {}))
            except ValueError:
                log.warning("не удалось прочитать %s", cfg)
        if field_map:
            self.field_map.update(field_map)
        self._form: Optional[parsers.FormSpec] = None
        self._search_html: Optional[str] = None

    # ------------------------------------------------------------------ helpers
    def url(self, path: str) -> str:
        return self.base_url + path

    def _load_search_form(self, force: bool = False) -> Optional[parsers.FormSpec]:
        """GET страницы поиска: получает cookies сессии и значения полей формы по умолчанию."""
        if self._form is not None and not force:
            return self._form
        res = self.http.get(self.url(SEARCH_PATH), use_cache=False)
        self._search_html = res.text
        self._form = parsers.parse_search_form(res.text, self.base_url)
        if self._form is None:
            log.warning("форма поиска не найдена на %s (возможно, рендерится JS) -- используем FIELD_MAP как есть", SEARCH_PATH)
        return self._form

    def event_type_options(self) -> list[dict]:
        form = self._load_search_form()
        return form.event_type_options() if form else []

    # ------------------------------------------------------------------ discover
    def discover(self, out_dir: Optional[Path] = None, sample_event: bool = True) -> dict:
        """Скачивает ключевые страницы, сохраняет HTML и сводку структуры (формы, ссылки, ajax-эндпоинты)."""
        out_dir = Path(out_dir or self.settings.discovery_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary: dict = {"base_url": self.base_url, "fetched_at": datetime.now().isoformat(timespec="seconds"), "pages": {}}

        def _save(name: str, html: str) -> None:
            (out_dir / f"{name}.html").write_text(html, "utf-8")

        # 1. страница поиска и форма
        try:
            form = self._load_search_form(force=True)
            _save("search_page", self._search_html or "")
            page_info = {"status": "ok", "links_portal": parsers.find_links(self._search_html or "", self.base_url, r"/portal/|poisk"),
                         "script_endpoints": parsers.find_script_endpoints(self._search_html or "")}
            if form:
                page_info["form"] = {"action": form.action, "method": form.method, "fields": form.fields,
                                     "checkbox_groups": {k: v[:200] for k, v in form.checkbox_groups.items()},
                                     "radio_groups": form.radio_groups, "selects": form.selects,
                                     "event_type_options": form.event_type_options()[:300]}
            summary["pages"]["search"] = page_info
        except HttpError as e:
            summary["pages"]["search"] = {"status": f"error: {e}"}

        # 2. лента последних сообщений
        rows: list[MessageRow] = []
        try:
            res = self.http.get(self.url(LASTNEWS_PATH), use_cache=False)
            _save("lastnews", res.text)
            rows = parsers.parse_lastnews(res.text, self.base_url)
            summary["pages"]["lastnews"] = {"status": "ok", "rows_parsed": len(rows),
                                            "sample": [r.to_dict() for r in rows[:5]],
                                            "script_endpoints": parsers.find_script_endpoints(res.text)}
        except HttpError as e:
            summary["pages"]["lastnews"] = {"status": f"error: {e}"}

        # 3. пример события и компании
        if sample_event and rows:
            r0 = rows[0]
            try:
                ev_html = self.http.get(self.url(EVENT_PATH), params={"EventId": r0.event_id}, use_cache=False).text
                _save("event_sample", ev_html)
                ev = parsers.parse_event_page(ev_html, r0.event_id)
                summary["pages"]["event"] = {"status": "ok", "event_id": r0.event_id, "parsed": {
                    "title": ev.title, "company_id": ev.company_id, "company_name": ev.company_name,
                    "published_at": ev.published_at.isoformat() if ev.published_at else None,
                    "event_type": ev.event_type, "body_len": len(ev.body_text), "body_head": ev.body_text[:800]}}
                cid = ev.company_id or r0.company_id
                if cid:
                    c_html = self.http.get(self.url(COMPANY_PATH), params={"id": cid}, use_cache=False).text
                    _save("company_sample", c_html)
                    ci = parsers.parse_company_page(c_html, cid)
                    summary["pages"]["company"] = {"status": "ok", "parsed": ci.to_dict(),
                                                   "links_portal": parsers.find_links(c_html, self.base_url, r"/portal/")[:100],
                                                   "script_endpoints": parsers.find_script_endpoints(c_html)}
            except HttpError as e:
                summary["pages"]["event"] = {"status": f"error: {e}"}

        (out_dir / "discovery.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), "utf-8")
        log.info("discovery сохранён в %s", out_dir)
        return summary

    # ------------------------------------------------------------------ search
    def _build_search_payload(self, date_from: date, date_till: date, page: int, page_size: int,
                              event_type_ids: Optional[Iterable[str]], query: Optional[str],
                              query_id: Optional[int]) -> dict:
        form = self._load_search_form()
        payload: dict = dict(form.fields) if form else {}
        fm = self.field_map
        payload[fm["date_from"]] = _fmt_date(date_from)
        payload[fm["date_till"]] = _fmt_date(date_till)
        payload[fm["page"]] = str(page)
        payload[fm["page_size"]] = str(page_size)
        if query is not None:
            payload[fm["query"]] = query
        if query_id is not None:
            payload[fm["query_id"]] = str(query_id)
        if event_type_ids:
            payload[fm["event_types"]] = [str(x) for x in event_type_ids]
        # радио-кнопки: значения по умолчанию (checked)
        if form:
            for name, opts in form.radio_groups.items():
                checked = [o for o in opts if o["checked"]]
                if checked and name not in payload:
                    payload[name] = checked[0]["value"]
        return payload

    def search_page(self, date_from: date, date_till: date, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                    event_type_ids: Optional[Iterable[str]] = None, query: Optional[str] = None,
                    query_id: Optional[int] = None, use_cache: bool = True) -> tuple[list[MessageRow], str]:
        """Одна страница результатов поиска. Возвращает (строки, html)."""
        payload = self._build_search_payload(date_from, date_till, page, page_size, event_type_ids, query, query_id)
        form = self._form
        action = form.action if form and form.action else self.url(SEARCH_PATH)
        method = (form.method if form else "post").upper()
        if method == "GET":
            res = self.http.request("GET", action, params=payload, use_cache=use_cache)
        else:
            res = self.http.request("POST", action, data=payload, use_cache=use_cache,
                                    headers={"Referer": self.url(SEARCH_PATH), "X-Requested-With": "XMLHttpRequest"})
        rows = parsers.parse_search_results(res.text, self.base_url)
        return rows, res.text

    def iter_search(self, date_from: date, date_till: date, chunk_days: int = 1,
                    event_type_ids: Optional[Iterable[str]] = None, query: Optional[str] = None,
                    query_id: Optional[int] = None, page_size: int = DEFAULT_PAGE_SIZE,
                    use_cache: bool = True) -> Iterator[MessageRow]:
        """Обход диапазона дат кусками по chunk_days дней с пагинацией внутри каждого куска.

        Останавливается на пустой странице, на странице короче page_size или при повторе всех id (защита от
        сайта, игнорирующего номер страницы).
        """
        cur = date_from
        while cur <= date_till:
            chunk_end = min(cur + timedelta(days=chunk_days - 1), date_till)
            seen: set[str] = set()
            for page in range(1, MAX_PAGES_PER_CHUNK + 1):
                rows, _ = self.search_page(cur, chunk_end, page, page_size, event_type_ids, query, query_id, use_cache)
                new = [r for r in rows if r.event_id not in seen]
                if not rows or not new:
                    break
                for r in new:
                    seen.add(r.event_id)
                    yield r
                if len(rows) < page_size:
                    break
            log.info("поиск %s..%s: %d сообщений", cur, chunk_end, len(seen))
            cur = chunk_end + timedelta(days=1)

    # ------------------------------------------------------------------ pages
    def lastnews(self) -> list[MessageRow]:
        res = self.http.get(self.url(LASTNEWS_PATH), use_cache=False)
        return parsers.parse_lastnews(res.text, self.base_url)

    def get_event(self, event_id: str) -> EventPage:
        res = self.http.get(self.url(EVENT_PATH), params={"EventId": event_id})
        ev = parsers.parse_event_page(res.text, event_id)
        return ev

    def get_event_html(self, event_id: str) -> str:
        return self.http.get(self.url(EVENT_PATH), params={"EventId": event_id}).text

    def get_company(self, company_id: int) -> CompanyInfo:
        res = self.http.get(self.url(COMPANY_PATH), params={"id": company_id})
        info = parsers.parse_company_page(res.text, company_id)
        info.company_id = company_id
        return info

    def find_companies(self, query: str) -> list[CompanyInfo]:
        """Поиск компании по названию/ИНН через страницу поиска по компаниям (структура не подтверждена).

        Возвращает компании, найденные по ссылкам company.aspx?id=... на странице результатов.
        """
        try:
            res = self.http.request("POST", self.url(COMPANY_SEARCH_PATH), data={"query": query, "lastPageNumber": "1", "lastPageSize": "20"})
        except HttpError:
            res = self.http.get(self.url(COMPANY_SEARCH_PATH), params={"query": query})
        soup = parsers.soup_of(res.text)
        out: list[CompanyInfo] = []
        seen: set[int] = set()
        for a in soup.find_all("a", href=True):
            m = parsers.COMPANY_ID_RE.search(a["href"])
            if not m:
                continue
            cid = int(m.group(1))
            if cid in seen:
                continue
            seen.add(cid)
            row_text = parsers.clean_text((a.find_parent("tr") or a.find_parent("div") or a).get_text(" "))
            inn = parsers.INN_RE.search(row_text)
            digits = parsers.re.search(r"\b(\d{10}|\d{12})\b", row_text)
            out.append(CompanyInfo(company_id=cid, name=parsers.clean_text(a.get_text(" ")),
                                   inn=inn.group(1) if inn else (digits.group(1) if digits else None)))
        return out
