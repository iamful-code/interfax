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
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional
from urllib.parse import urlsplit, urlunsplit

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
                 field_map: Optional[dict] = None, form_config_path: Optional[Path] = None,
                 use_browser: Optional[bool] = None):
        self.settings = settings or load_settings()
        self.base_url = self.settings.edisclosure_base_url.rstrip("/")
        self.owns_transport = http is None
        if http is None and (self.settings.use_browser if use_browser is None else use_browser):
            from ..browser import BrowserTransport

            http = BrowserTransport(
                warmup_url=self.base_url + SEARCH_PATH,
                min_interval_sec=self.settings.edisclosure_min_interval_sec,
                timeout_sec=self.settings.timeout_sec,
                cache_dir=self.settings.cache_dir / "edisclosure",
                headless=self.settings.browser_headless,
                user_data_dir=self.settings.browser_profile_dir,
                stub_detector=is_protection_stub,
                captcha_detector=is_captcha_page,
                auto_visible_on_captcha=self.settings.browser_auto_visible_on_captcha,
                warmup_timeout_sec=self.settings.browser_warmup_timeout_sec,
                navigate_for_get=self.settings.browser_navigate_get,
                settle_ms=self.settings.browser_settle_ms,
            )
        self.http = http or HttpClient(
            min_interval_sec=self.settings.edisclosure_min_interval_sec,
            user_agent=self.settings.user_agent,
            timeout_sec=self.settings.timeout_sec,
            max_retries=self.settings.max_retries,
            cache_dir=self.settings.cache_dir / "edisclosure",
            cookies_file=self.settings.cookies_file,
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
    def close(self) -> None:
        """Закрывает транспорт, если он наш (актуально для браузерного режима)."""
        if self.owns_transport and hasattr(self.http, "close"):
            self.http.close()

    def __enter__(self) -> "EDisclosureClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_browser(self) -> bool:
        return type(self.http).__name__ == "BrowserTransport"

    def url(self, path: str) -> str:
        return self.base_url + path

    def _load_search_form(self, force: bool = False) -> Optional[parsers.FormSpec]:
        """GET страницы поиска: получает cookies сессии и значения полей формы по умолчанию."""
        if self._form is not None and not force:
            return self._form
        res = self.http.get(self.url(SEARCH_PATH), use_cache=False)
        self._search_html = res.text
        if is_protection_stub(res.text):
            raise HttpError(res.url, res.status,
                            "страница поиска отдаёт заглушку антибот-защиты; нужны cookies браузера в config/cookies.txt",
                            body=res.text)
        self._form = parsers.parse_search_form(res.text, self.base_url, page_url=self.url(SEARCH_PATH))
        if self._form is None:
            log.warning("форма поиска не найдена на %s (возможно, рендерится JS) -- используем FIELD_MAP как есть", SEARCH_PATH)
        return self._form

    def event_type_options(self) -> list[dict]:
        form = self._load_search_form()
        return form.event_type_options() if form else []

    # ------------------------------------------------------------------ discover
    def _probe(self, name: str, url: str, out_dir: Path, params: Optional[dict] = None) -> tuple[Optional[str], dict]:
        """GET страницы для discover: при ошибке сохраняет тело/заголовки ответа и описывает причину."""
        try:
            res = self.http.get(url, params=params, use_cache=False)
            (out_dir / f"{name}.html").write_text(res.text, "utf-8")
            kind = classify_page(res.text)
            if kind == "captcha":
                return None, {"status": "капча (нужно пройти вручную)", "http_status": res.status, "url": res.url,
                              "captcha": True, "stub": True, "saved_file": str(out_dir / f"{name}.html"),
                              "dom": parsers.describe_dom(res.text),
                              "diagnosis": "сайт показывает капчу Servicepipe. Запустите команду с видимым окном "
                                           "(--show-browser), пройдите капчу в нём -- профиль браузера сохранится "
                                           "в data/browser_profile, дальше команды пойдут без неё"}
            if kind == "stub":
                return None, {"status": "заглушка антибот-защиты (HTTP 200, но контента нет)",
                              "http_status": res.status, "url": res.url, "stub": True,
                              "saved_file": str(out_dir / f"{name}.html"),
                              "dom": parsers.describe_dom(res.text),
                              "diagnosis": "JS-проверка браузера (Servicepipe): страница отдаёт спиннер и скрипт, "
                                           "который выставляет cookies spjs/spsc/spid. Проще всего пройти её "
                                           "режимом --browser (см. docs/windows_quickstart.md)"}
            return res.text, {"status": "ok", "http_status": res.status, "url": res.url}
        except Exception as e:  # noqa: BLE001 -- диагностика: любая ошибка должна попасть в отчёт
            if type(e).__name__ == "CaptchaRequired":
                return None, {"status": "капча (нужно пройти вручную)", "url": url, "captcha": True, "stub": True,
                              "diagnosis": str(e)}
            if not isinstance(e, HttpError):
                raise
            info: dict = {"status": f"error: HTTP {e.status}", "http_status": e.status, "url": url}
            if e.body:
                (out_dir / f"{name}_error.html").write_text(e.body, "utf-8")
                info["error_file"] = str(out_dir / f"{name}_error.html")
                info["body_head"] = parsers.clean_text(parsers.soup_of(e.body).get_text(" "))[:400]
            keep = ("server", "retry-after", "set-cookie", "content-type", "cf-ray", "x-powered-by", "via", "location")
            info["headers"] = {k: v for k, v in e.headers.items() if k.lower() in keep}
            info["diagnosis"] = diagnose_block(e.status, e.body or "", e.headers)
            return None, info

    FEED_PATH_HINTS = ("lenta", "novost", "news", "soobsh", "feed", "raskrytie")

    def _feed_candidates(self, summary: dict) -> list[str]:
        """Кандидаты в адрес ленты сообщений: ссылки со страницы поиска, похожие на ленту новостей."""
        dom = (summary.get("pages", {}).get("search") or {}).get("dom") or {}
        out: list[str] = []
        for a in dom.get("anchors_sample", []):
            href, text = a.get("href", ""), (a.get("text") or "").lower()
            if not href or href.startswith(("#", "mailto:", "javascript:")):
                continue
            hay = (href + " " + text).lower()
            if any(h in hay for h in self.FEED_PATH_HINTS) and href not in out:
                out.append(href)
        return out

    def discover(self, out_dir: Optional[Path] = None, sample_event: bool = True, quick: bool = True) -> dict:
        """Скачивает ключевые страницы, сохраняет HTML и сводку структуры (формы, ссылки, ajax-эндпоинты).

        При ошибках (403/503 -- защита или техработы) сохраняет страницу ошибки, определяет тип защиты и
        пробует альтернативный хост (с/без www.). ``quick`` -- не ждать долгих повторов (1 повтор вместо 4).
        """
        out_dir = Path(out_dir or self.settings.discovery_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary: dict = {"base_url": self.base_url, "transport": "browser" if self.is_browser else "http",
                         "fetched_at": datetime.now().isoformat(timespec="seconds"), "pages": {}, "hints": []}
        prev_retries = self.http.max_retries
        if quick:
            self.http.max_retries = 1
        try:
            # 1. страница поиска и форма (при неудаче -- альтернативный хост)
            html, info = self._probe("search_page", self.url(SEARCH_PATH), out_dir)
            if html is None:
                alt = alternate_host(self.base_url)
                alt_html, alt_info = self._probe("search_page_althost", alt + SEARCH_PATH, out_dir)
                info["alternate_host"] = {"base_url": alt, **alt_info}
                if alt_html is not None:
                    summary["hints"].append(f"Хост {alt} отвечает, а {self.base_url} -- нет: задайте DA_EDISCLOSURE_BASE_URL={alt}")
                    html = alt_html
            if html is not None:
                self._search_html = html
                form = parsers.parse_search_form(html, self.base_url, page_url=self.url(SEARCH_PATH))
                self._form = form
                info["dom"] = parsers.describe_dom(html)
                info["links_portal"] = parsers.find_links(html, self.base_url, r"/portal/|poisk")
                if form:
                    info["form"] = {"action": form.action, "method": form.method, "fields": form.fields,
                                    "checkbox_groups": {k: v[:200] for k, v in form.checkbox_groups.items()},
                                    "radio_groups": form.radio_groups, "selects": form.selects,
                                    "event_type_options": form.event_type_options()[:300]}
                else:
                    summary["hints"].append("На странице поиска не найдена форма -- вероятно, она строится скриптом; пришлите search_page.html")
            summary["pages"]["search"] = info

            # 2. лента последних сообщений: старый адрес мог смениться -- пробуем кандидатов
            rows: list[MessageRow] = []
            feed_candidates = [LASTNEWS_PATH] + [c for c in self._feed_candidates(summary) if c != LASTNEWS_PATH]
            html, info = None, {"status": "не пробовали"}
            for i, path in enumerate(feed_candidates[:5]):
                html, info = self._probe("lastnews" if i == 0 else f"feed_{i}", self.url(path) if path.startswith("/") else path, out_dir)
                info["path"] = path
                if html is not None and parsers.parse_message_list(html, self.base_url):
                    break
            summary["feed_candidates_tried"] = [c for c in feed_candidates[:5]]
            if html is not None:
                rows = parsers.parse_lastnews(html, self.base_url)
                info.update({"rows_parsed": len(rows), "sample": [r.to_dict() for r in rows[:5]],
                             "dom": parsers.describe_dom(html)})
            summary["pages"]["lastnews"] = info

            # 3. пример события и компании
            if sample_event and rows:
                r0 = rows[0]
                ev_html, info = self._probe("event_sample", self.url(EVENT_PATH), out_dir, params={"EventId": r0.event_id})
                if ev_html is not None:
                    info["dom"] = parsers.describe_dom(ev_html)
                    ev = parsers.parse_event_page(ev_html, r0.event_id)
                    info.update({"event_id": r0.event_id, "parsed": {
                        "title": ev.title, "company_id": ev.company_id, "company_name": ev.company_name,
                        "published_at": ev.published_at.isoformat() if ev.published_at else None,
                        "event_type": ev.event_type, "body_len": len(ev.body_text), "body_head": ev.body_text[:800]}})
                    cid = ev.company_id or r0.company_id
                    if cid:
                        c_html, c_info = self._probe("company_sample", self.url(COMPANY_PATH), out_dir, params={"id": cid})
                        if c_html is not None:
                            ci = parsers.parse_company_page(c_html, cid)
                            c_info.update({"parsed": ci.to_dict(), "dom": parsers.describe_dom(c_html),
                                           "links_portal": parsers.find_links(c_html, self.base_url, r"/portal/")[:100]})
                        summary["pages"]["company"] = c_info
                summary["pages"]["event"] = info
        finally:
            self.http.max_retries = prev_retries

        # значения известны только после первого запроса (браузер стартует лениво)
        summary["user_agent"] = self.http.session.headers.get("User-Agent")
        summary["cookies_loaded"] = getattr(self.http, "cookies_loaded", 0)
        summary["placeholder_cookies"] = getattr(self.http, "placeholder_cookies", [])
        if summary.get("placeholder_cookies"):
            summary["hints"].insert(0, "в config/cookies.txt остались шаблонные значения ("
                                    + ", ".join(summary["placeholder_cookies"])
                                    + "): подставьте настоящие из браузера или используйте режим --browser")
        if any(p.get("captcha") for p in summary["pages"].values()):
            summary["hints"].insert(0, "показана капча: запустите ту же команду с --show-browser и пройдите её "
                                       "в открывшемся окне (один раз, профиль сохранится в data/browser_profile)"
                                    if self.settings.browser_headless else
                                    "капча не пройдена: пройдите её в открытом окне браузера, команда продолжит сама")
        if any(p.get("stub") for p in summary["pages"].values()) and not self.is_browser:
            summary["hints"].append("проверку браузера можно проходить автоматически: "
                                    "pip install playwright && playwright install chromium, затем disclosure-alpha discover --browser")
        for name, page in summary["pages"].items():
            diag = page.get("diagnosis")
            if diag:
                summary["hints"].append(f"{name}: {diag}")
        summary["hints"] = list(dict.fromkeys(summary["hints"]))
        (out_dir / "discovery.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), "utf-8")
        log.info("discovery сохранён в %s", out_dir)
        return summary

    # ------------------------------------------------------------------ поиск через саму форму (браузер)
    SEARCH_BUTTON_SELECTORS = ("#sEventSearchForm__button-search", ".sEventSearchForm__button-search",
                               "[id*='button-search']", "[class*='button-search']",
                               "form button[type=submit]", "form input[type=submit]",
                               "button[type=submit]", "input[type=submit]")
    CONFIRM_BUTTON_SELECTORS = ("#sEventSearchForm__button-confirm", ".sEventSearchForm__button-confirm")
    # ждать нужно именно ссылки на сообщения: таблицы на странице есть и до поиска (дерево типов, календарь)
    RESULTS_SELECTORS = ("a[href*='EventId']", "a[href*='/event/']", "a[href*='soobshhenie']",
                         "a[href*='/message']", "#searchResults a", "[class*=searchResult] a")

    def search_via_form(self, date_from: date, date_till: date, event_type_ids: Optional[Iterable[str]] = None,
                        query: Optional[str] = None, wait_selector: Optional[str] = None) -> tuple[list[MessageRow], str]:
        """Заполняет форму поиска прямо в браузере и нажимает «Найти». Возвращает (строки, HTML результатов).

        Используется в браузерном режиме, потому что форму отправляет скрипт страницы, а не обычный POST.
        """
        if not self.is_browser:
            raise RuntimeError("search_via_form доступен только в браузерном режиме (--browser)")
        tr = self.http
        tr.open_page(self.url(SEARCH_PATH))
        fm = self.field_map
        filled = {
            fm["date_from"]: tr.fill_field(fm["date_from"], _fmt_date(date_from)),
            fm["date_till"]: tr.fill_field(fm["date_till"], _fmt_date(date_till)),
        }
        if query:
            filled[fm["query"]] = tr.fill_field(fm["query"], query)
        checked = 0
        if event_type_ids:
            checked = tr.set_checkbox_group(fm["event_types"], [str(x) for x in event_type_ids])
            tr.click_first(list(self.CONFIRM_BUTTON_SELECTORS))
        log.info("форма поиска: заполнено %s, отмечено типов: %d", filled, checked)
        closed = tr.dismiss_overlays()
        if closed:
            log.info("закрыты перекрывающие баннеры: %s", closed)
        clicked = tr.click_first(list(self.SEARCH_BUTTON_SELECTORS))
        if not clicked:
            clickables = tr.list_clickables()
            self._save_diagnostic("search_button_not_found.json", {"selectors_tried": list(self.SEARCH_BUTTON_SELECTORS),
                                                                   "clickables": clickables})
            visible = [c for c in clickables if c.get("visible")]
            raise HttpError(self.url(SEARCH_PATH), 0,
                            "не найдена кнопка поиска. Видимые кнопки на странице: "
                            + "; ".join(f"{c['tag']}#{c['id']}.{c['cls']} {c['text']!r}" for c in visible[:10]))
        log.info("нажата кнопка поиска: %s", clicked)
        tr.wait_for_selector(wait_selector or ", ".join(self.RESULTS_SELECTORS), timeout_ms=20_000)
        html = tr.current_html()
        return parsers.parse_search_results(html, self.base_url), html

    def _save_diagnostic(self, name: str, data: dict) -> Path:
        out = Path(self.settings.discovery_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), "utf-8")
        log.info("диагностика сохранена: %s", path)
        return path

    def probe_search(self, date_from: date, date_till: date, event_type_ids: Optional[Iterable[str]] = None,
                     out_dir: Optional[Path] = None) -> dict:
        """Разведка результатов поиска: выполняет запрос через форму и описывает разметку ответа."""
        out_dir = Path(out_dir or self.settings.discovery_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        self.http.start_recording()
        rows, html = self.search_via_form(date_from, date_till, event_type_ids)
        (out_dir / "search_results.html").write_text(html, "utf-8")
        requests_log = self.http.recorded_requests()
        summary = {"date_from": str(date_from), "date_till": str(date_till),
                   "event_type_ids": list(event_type_ids or []), "rows_parsed": len(rows),
                   "sample": [r.to_dict() for r in rows[:5]], "dom": parsers.describe_dom(html),
                   "field_names": self.http.field_names(), "clickables": self.http.list_clickables(30),
                   "page_url": getattr(self.http, "_page", None) and self.http._page.url,
                   "requests": requests_log}
        (out_dir / "search_probe.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), "utf-8")
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
        payload.pop(fm.get("page_size_alias", ""), None)
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
        res = self._send_search(payload, use_cache)
        if is_protection_stub(res.text):
            raise HttpError(res.url, res.status, "получена заглушка антибот-защиты: обновите config/cookies.txt", body=res.text)
        rows = parsers.parse_search_results(res.text, self.base_url)
        return rows, res.text

    def _send_search(self, payload: dict, use_cache: bool, _retry: bool = True):
        """Отправка формы поиска. При отказе из-за устаревшего antiforgery-токена перечитывает форму и повторяет."""
        form = self._form
        action = form.action if form and form.action else self.url(SEARCH_PATH)
        method = (form.method if form else "post").upper()
        headers = {"Referer": self.url(SEARCH_PATH), "Sec-Fetch-Site": "same-origin", "Origin": self.base_url}
        token = next((v for k, v in payload.items() if "requestverificationtoken" in k.lower()), None)
        if token:
            headers["RequestVerificationToken"] = token          # ASP.NET Core принимает токен и заголовком
        try:
            if method == "GET":
                return self.http.request("GET", action, params=payload, use_cache=use_cache, headers=headers)
            return self.http.request("POST", action, data=payload, use_cache=use_cache, headers=headers)
        except HttpError as e:
            if _retry and e.status in (400, 403, 419):
                log.info("поиск отклонён (HTTP %s) -- перечитываем форму и повторяем", e.status)
                self._load_search_form(force=True)
                fresh = self._form
                retry_payload = dict(payload)          # не меняем словарь вызывающего
                if fresh:
                    for k, v in fresh.fields.items():
                        if "requestverificationtoken" in k.lower() or k not in retry_payload:
                            retry_payload[k] = v
                return self._send_search(retry_payload, use_cache, _retry=False)
            raise

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


def alternate_host(base_url: str) -> str:
    """https://e-disclosure.ru <-> https://www.e-disclosure.ru"""
    parts = urlsplit(base_url)
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else "www." + parts.netloc
    return urlunsplit((parts.scheme, host, "", "", ""))


# Признаки страницы-заглушки антибот-защиты: контента нет, есть спиннер/блок капчи и обфусцированный скрипт,
# который вычисляет cookies (у e-disclosure -- Servicepipe: cookies spjs/spsc/spid) и перезагружает страницу.
_STUB_MARKERS = ("id_captcha_frame_div", "spinner-container", "id_spinner", "__sp_init", "spsc=")


# Страница капчи Servicepipe: подключает свои скрипты и требует ручного прохождения
# Внимание: контейнер id_captcha_frame_div есть и на обычной странице JS-проверки (скрытый),
# поэтому признаком капчи считаем только подключение её скриптов и явный флаг is_captcha.
_CAPTCHA_MARKERS = ("sp_rotated_captcha", "captchaintgen", '"is_captcha":true', '"is_captcha": true')


def is_captcha_page(html: str) -> bool:
    """True, если вместо страницы сайта показана капча (нужно пройти вручную в видимом окне)."""
    if not html:
        return False
    head = html[:60_000].lower()
    return any(m in head for m in _CAPTCHA_MARKERS)


def classify_page(html: str) -> str:
    """content -- настоящая страница; captcha -- требуется ручное прохождение; stub -- JS-проверка."""
    if is_captcha_page(html):
        return "captcha"
    if is_protection_stub(html):
        return "stub"
    return "content"


def is_protection_stub(html: str) -> bool:
    """True, если вместо страницы сайта пришла заглушка проверки браузера."""
    if not html:
        return False
    head = html[:40_000].lower()
    if any(m.lower() in head for m in _STUB_MARKERS) or is_captcha_page(html):
        return True
    no_links = "<a " not in html.lower()
    js_redirect = 'http-equiv="refresh"' in head and "<noscript>" in head
    return no_links and js_redirect and "<script" in head


_PROTECTION_MARKERS = [
    (r"ddos-guard|__ddg", "DDoS-Guard (JS-проверка браузера)"),
    (r"id_captcha_frame_div|spsc=|spjs=|__sp_init", "Servicepipe (JS-проверка браузера, cookies spjs/spsc/spid)"),
    (r"qrator", "Qrator (антибот-проверка)"),
    (r"variti", "Variti (антибот-проверка)"),
    (r"servicepipe", "Servicepipe (антибот-проверка)"),
    (r"cloudflare|cf-ray|cf_chl", "Cloudflare (проверка браузера)"),
    (r"captcha", "капча"),
    (r"технические работы|техническ\S+ работ|maintenance|временно недоступен", "техработы / сервис временно недоступен"),
    (r"слишком много запросов|too many requests|превышен", "ограничение частоты запросов"),
    (r"document\.cookie|setcookie|challenge", "JS-проверка, выставляющая cookie"),
]


def diagnose_block(status: int, body: str, headers: Optional[dict] = None) -> str:
    """Короткий вердикт по ответу с ошибкой: тип защиты / техработы / гео-блок и что делать."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    text = (body or "")[:200_000].lower() + " " + " ".join(f"{k}: {v}".lower() for k, v in headers.items())
    found = [label for pat, label in _PROTECTION_MARKERS if re.search(pat, text)]
    parts = [f"HTTP {status}"]
    if found:
        parts.append("признаки: " + ", ".join(dict.fromkeys(found)))
    if headers.get("retry-after"):
        parts.append(f"Retry-After={headers['retry-after']}")
    if "set-cookie" in headers:
        parts.append("сервер ставит cookie")
    if status == 403 and not found:
        parts.append("возможна гео-блокировка (нужен российский IP)")
    if status in (403, 503):
        parts.append("если сайт открывается в браузере -- сохраните cookies браузера в config/cookies.txt и "
                     "задайте DA_USER_AGENT как в браузере; иначе это техработы, повторите позже")
    return "; ".join(parts)
