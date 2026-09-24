"""Транспорт через настоящий браузер (Playwright + Chromium).

Зачем: перед e-disclosure.ru стоит защита с JS-проверкой браузера -- обычный HTTP-клиент получает
страницу-заглушку. Браузер проверку проходит сам, выставляет нужные cookies, после чего остальные
запросы идут через сетевой стек браузера (те же cookies и TLS-отпечаток), что и обычная навигация.

Класс :class:`BrowserTransport` повторяет публичный интерфейс :class:`~disclosure_alpha.http.HttpClient`
(get / post / request / get_text / get_json, ``session.headers``, ``max_retries``), поэтому его можно
передать в ``EDisclosureClient(http=...)`` вместо обычного клиента.

Установка на стороне пользователя::

    pip install playwright
    playwright install chromium
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode

from .http import DiskCache, FetchResult, HttpError, _cache_key

log = logging.getLogger(__name__)

DEFAULT_WARMUP_TIMEOUT_SEC = 60.0
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


# Аргументы запуска, убирающие явные признаки автоматизации
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
IGNORE_DEFAULT_ARGS = ["--enable-automation"]


_NAVIGATION_ERROR_MARKERS = ("navigating and changing the content", "execution context was destroyed",
                             "target closed", "frame was detached", "navigation")


def _is_navigation_error(exc: BaseException) -> bool:
    """Ошибки Playwright, означающие «страница сейчас перезагружается», а не настоящий сбой."""
    return any(m in str(exc).lower() for m in _NAVIGATION_ERROR_MARKERS)


def normal_user_agent(ua: str) -> str:
    """HeadlessChrome в User-Agent -- явный признак робота; подменяем на обычный Chrome."""
    return (ua or "").replace("HeadlessChrome", "Chrome")


class BrowserUnavailable(RuntimeError):
    """Playwright или браузер не установлены."""


class _HeaderShim:
    """Минимальная замена requests.Session для совместимости (используется только .headers)."""

    def __init__(self, headers: dict):
        self.headers = headers


def find_chromium(explicit: Optional[str] = None) -> Optional[str]:
    """Путь к исполняемому файлу Chromium.

    Обычно Playwright находит браузер сам; функция нужна, когда установленная сборка браузера
    отличается от ожидаемой версией (тогда ищем любую подходящую в каталоге браузеров).
    """
    if explicit:
        return explicit
    env = os.environ.get("DA_BROWSER_EXECUTABLE")
    if env:
        return env
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            expected = Path(pw.chromium.executable_path)
        if expected.exists():
            return None  # штатный путь -- пусть Playwright решает сам
        root = expected.parents[2] if len(expected.parents) >= 3 else None
    except Exception:  # noqa: BLE001
        root = None
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(root or ""))) if (root or os.environ.get("PLAYWRIGHT_BROWSERS_PATH")) else None
    if not root or not root.exists():
        return None
    for pattern in ("chromium-*/chrome-linux*/chrome", "chromium-*/chrome-win*/chrome.exe",
                    "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                    "chromium_headless_shell-*/chrome-linux*/headless_shell"):
        found = sorted(root.glob(pattern))
        if found:
            return str(found[-1])
    return None


class BrowserTransport:
    """HTTP через Chromium под управлением Playwright.

    Порядок работы: ``start()`` поднимает браузер и открывает стартовую страницу (``warmup_url``),
    дожидаясь, пока пройдёт JS-проверка; дальше ``get``/``post`` идут через сетевой контекст браузера.
    Если ответ снова выглядит заглушкой (``stub_detector``), выполняется повторный прогрев и попытка.
    """

    def __init__(
        self,
        warmup_url: str,
        min_interval_sec: float = 1.0,
        timeout_sec: float = 60.0,
        max_retries: int = 2,
        cache_dir: Optional[Path] = None,
        cache_ttl_sec: Optional[float] = None,
        headless: bool = True,
        executable_path: Optional[str] = None,
        user_data_dir: Optional[Path] = None,
        locale: str = "ru-RU",
        user_agent: Optional[str] = None,
        stub_detector: Optional[Callable[[str], bool]] = None,
        warmup_timeout_sec: float = DEFAULT_WARMUP_TIMEOUT_SEC,
        navigate_for_get: bool = True,
    ):
        self.warmup_url = warmup_url
        self.min_interval_sec = min_interval_sec
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self._cache = DiskCache(Path(cache_dir) if cache_dir else None, cache_ttl_sec)
        self.headless = headless
        self.executable_path = find_chromium(executable_path)
        self.user_data_dir = Path(user_data_dir) if user_data_dir else None
        self.locale = locale
        self.user_agent = user_agent
        self.stub_detector = stub_detector or (lambda _html: False)
        self.warmup_timeout_sec = warmup_timeout_sec
        # GET выполняем настоящей навигацией: защита отличает переход по ссылке от программного запроса
        self.navigate_for_get = navigate_for_get

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_request_ts = 0.0
        self.cookies_loaded = 0
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "warmups": 0}
        self.session = _HeaderShim({"User-Agent": user_agent or ""})

    # ------------------------------------------------------------------ жизненный цикл
    def start(self) -> "BrowserTransport":
        if self._context is not None:
            return self
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover -- зависит от окружения
            raise BrowserUnavailable(
                "не установлен Playwright. Выполните: pip install playwright && playwright install chromium") from exc
        self._pw = sync_playwright().start()
        self._launch(self.user_agent)
        real_ua = self._read_user_agent()
        if self.user_agent is None and normal_user_agent(real_ua) != real_ua:
            # headless выдаёт себя строкой HeadlessChrome -- перезапускаем с обычным User-Agent
            self.user_agent = normal_user_agent(real_ua)
            self._close_browser()
            self._launch(self.user_agent)
            real_ua = self._read_user_agent()
        self.session.headers["User-Agent"] = real_ua
        self.warmup()
        return self

    def _launch(self, user_agent: Optional[str]) -> None:
        launch: dict[str, Any] = {"headless": self.headless, "args": LAUNCH_ARGS,
                                  "ignore_default_args": IGNORE_DEFAULT_ARGS}
        if self.executable_path:
            launch["executable_path"] = self.executable_path
        try:
            if self.user_data_dir:            # постоянный профиль: cookies проверки переживают перезапуск
                self.user_data_dir.mkdir(parents=True, exist_ok=True)
                self._context = self._pw.chromium.launch_persistent_context(
                    str(self.user_data_dir), locale=self.locale, user_agent=user_agent, **launch)
                self._browser = None
            else:
                self._browser = self._pw.chromium.launch(**launch)
                self._context = self._browser.new_context(locale=self.locale, user_agent=user_agent)
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise BrowserUnavailable(
                f"не удалось запустить Chromium ({exc}). Выполните: playwright install chromium") from exc
        self._context.set_default_timeout(self.timeout_sec * 1000)
        pages = getattr(self._context, "pages", None)
        self._page = pages[0] if pages else self._context.new_page()

    def _read_user_agent(self) -> str:
        try:
            return self._page.evaluate("() => navigator.userAgent") or ""
        except Exception as exc:  # noqa: BLE001
            if _is_navigation_error(exc):
                return self.user_agent or ""
            raise

    def _close_browser(self) -> None:
        for obj in (self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = self._context = self._page = None

    def _safe_content(self) -> Optional[str]:
        """HTML текущей страницы; None -- страница как раз перезагружается (так ведёт себя JS-проверка)."""
        for wait_for in ("domcontentloaded",):
            try:
                self._page.wait_for_load_state(wait_for, timeout=5000)
            except Exception:  # noqa: BLE001 -- таймаут ожидания не критичен
                pass
        try:
            return self._page.content()
        except Exception as exc:  # noqa: BLE001
            if _is_navigation_error(exc):
                return None
            raise

    def warmup(self) -> bool:
        """Открывает стартовую страницу и ждёт, пока пройдёт проверка браузера. True -- контент получен."""
        if self._page is None:
            self.start()
            return True
        self.stats["warmups"] += 1
        deadline = time.time() + self.warmup_timeout_sec
        try:
            self._page.goto(self.warmup_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001 -- проверка может увести страницу прямо во время перехода
            if not _is_navigation_error(exc):
                raise
        while time.time() < deadline:
            html = self._safe_content()
            if html is not None and not self.stub_detector(html):
                self.cookies_loaded = len(self._context.cookies())
                log.info("проверка браузера пройдена, cookies: %d", self.cookies_loaded)
                return True
            try:
                self._page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                time.sleep(1.0)
        log.warning("проверка браузера не завершилась за %.0f с: увеличьте DA_BROWSER_WARMUP_TIMEOUT "
                    "или запустите с видимым окном (--show-browser)", self.warmup_timeout_sec)
        return False

    def close(self) -> None:
        for obj, name in ((self._context, "context"), (self._browser, "browser"), (self._pw, "playwright")):
            if obj is None:
                continue
            try:
                obj.stop() if name == "playwright" else obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._context = self._page = None

    def __enter__(self) -> "BrowserTransport":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ запросы
    def _throttle(self) -> None:
        wait = self.min_interval_sec - (time.time() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait)

    def request(
        self,
        method: str,
        url: str,
        params: Optional[Mapping] = None,
        data: Optional[Mapping] = None,
        headers: Optional[Mapping[str, str]] = None,
        use_cache: bool = True,
        allow_redirects: bool = True,
        prefer_fetch: bool = False,
    ) -> FetchResult:
        key = _cache_key(method, url, params, data)
        if use_cache:
            cached = self._cache.get(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached
        if self._context is None:
            self.start()

        last_status = -1
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_ts = time.time()
            self.stats["requests"] += 1
            res = self._fetch_once(method, url, params, data, headers, prefer_fetch)
            last_status = res.status
            html_like = "html" in (res.headers.get("content-type", "") or "").lower() or res.content[:512].lstrip().startswith(b"<")
            if res.ok and html_like and self.stub_detector(res.text):
                if attempt < self.max_retries:
                    self.stats["retries"] += 1
                    log.info("получена заглушка проверки браузера -- повторяем прогрев")
                    self.warmup()
                    continue
                raise HttpError(url, res.status, "браузеру не удалось пройти проверку", body=res.text, headers=res.headers)
            if res.status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                self.stats["retries"] += 1
                time.sleep(min(2.0 ** attempt, 30.0))
                continue
            if not res.ok:
                raise HttpError(url, res.status, res.text[:120].replace("\n", " ").strip(), body=res.text, headers=res.headers)
            if use_cache:
                self._cache.put(key, res)
            return res
        raise HttpError(url, last_status, "исчерпаны попытки")

    def _fetch_once(self, method: str, url: str, params: Optional[Mapping], data: Optional[Mapping],
                    headers: Optional[Mapping[str, str]], prefer_fetch: bool = False) -> FetchResult:
        full = url + (("?" + urlencode(dict(params), doseq=True)) if params else "")
        if method.upper() == "GET" and self.navigate_for_get and not prefer_fetch:
            return self._fetch_by_navigation(full)
        return self._fetch_from_page(method, full, data, headers)

    def _wait_for_real_content(self, deadline: Optional[float] = None) -> str:
        """Ждёт, пока страница перестанет быть заглушкой проверки (она перезагружает себя сама)."""
        deadline = deadline or (time.time() + self.warmup_timeout_sec)
        html = ""
        while time.time() < deadline:
            current = self._safe_content()
            if current is not None:
                html = current
                if not self.stub_detector(html):
                    return html
            try:
                self._page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        return html

    def _fetch_by_navigation(self, url: str) -> FetchResult:
        """GET как обычный переход по ссылке: браузер сам проходит проверку и отдаёт готовый DOM."""
        resp = None
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            if not _is_navigation_error(exc):
                raise
        html = self._wait_for_real_content()
        status = 200
        resp_headers: dict = {}
        final_url = url
        try:
            if resp is not None:
                status = resp.status
                resp_headers = dict(resp.all_headers())
            final_url = self._page.url or url
        except Exception as exc:  # noqa: BLE001
            if not _is_navigation_error(exc):
                raise
        if self.stub_detector(html):
            status = status if status != 200 else 200      # заглушка приходит с кодом 200
        return FetchResult(url=final_url, status=status, content=html.encode("utf-8"),
                           headers=resp_headers or {"content-type": "text/html; charset=utf-8"}, encoding="utf-8")

    _FETCH_SCRIPT = """async (arg) => {
        const init = {method: arg.method, credentials: 'include', headers: arg.headers || {}};
        if (arg.body !== null && arg.body !== undefined) { init.body = arg.body; }
        const r = await fetch(arg.url, init);
        const text = await r.text();
        const headers = {};
        r.headers.forEach((v, k) => { headers[k] = v; });
        return {status: r.status, url: r.url, text: text, headers: headers};
    }"""

    def _ensure_same_origin(self, url: str) -> None:
        """fetch выполняется в контексте страницы, поэтому она должна быть того же происхождения."""
        target = url.split("/", 3)[:3]
        current = (self._page.url or "").split("/", 3)[:3]
        if current != target:
            try:
                self._page.goto("/".join(target) + "/", wait_until="domcontentloaded")
                self._wait_for_real_content()
            except Exception as exc:  # noqa: BLE001
                if not _is_navigation_error(exc):
                    raise

    def _fetch_from_page(self, method: str, url: str, data: Optional[Mapping],
                         headers: Optional[Mapping[str, str]]) -> FetchResult:
        """POST (и JSON-запросы) выполняются как fetch изнутри страницы: те же cookies, Origin и Referer."""
        self._ensure_same_origin(url)
        req_headers = {k: v for k, v in (headers or {}).items() if k.lower() not in ("referer", "origin", "host")}
        body = None
        if method.upper() != "GET":
            body = urlencode(dict(data or {}), doseq=True)
            req_headers.setdefault("content-type", FORM_CONTENT_TYPE)
        result = self._page.evaluate(self._FETCH_SCRIPT, {"method": method.upper(), "url": url,
                                                          "body": body, "headers": req_headers})
        text = result.get("text") or ""
        return FetchResult(url=result.get("url") or url, status=int(result.get("status", 0)),
                           content=text.encode("utf-8"), headers=dict(result.get("headers") or {}), encoding="utf-8")

    def get(self, url: str, params: Optional[Mapping] = None, **kw) -> FetchResult:
        return self.request("GET", url, params=params, **kw)

    def post(self, url: str, data: Optional[Mapping] = None, params: Optional[Mapping] = None, **kw) -> FetchResult:
        kw.setdefault("use_cache", False)
        return self.request("POST", url, params=params, data=data, **kw)

    def get_text(self, url: str, params: Optional[Mapping] = None, **kw) -> str:
        return self.get(url, params=params, **kw).text

    def get_json(self, url: str, params: Optional[Mapping] = None, **kw) -> Any:
        kw.setdefault("prefer_fetch", True)      # JSON нельзя брать из DOM -- запрашиваем через fetch
        return self.get(url, params=params, **kw).json()
