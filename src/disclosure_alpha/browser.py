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


class CaptchaRequired(RuntimeError):
    """Сайт показал капчу, а окно браузера скрыто -- пройти её некому."""


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
        captcha_detector: Optional[Callable[[str], bool]] = None,
        captcha_timeout_sec: float = 300.0,
        auto_visible_on_captcha: bool = True,
        warmup_timeout_sec: float = DEFAULT_WARMUP_TIMEOUT_SEC,
        navigate_for_get: bool = True,
        settle_ms: int = 1500,
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
        self.captcha_detector = captcha_detector or (lambda _html: False)
        self.captcha_timeout_sec = captcha_timeout_sec
        # при капче в скрытом режиме открываем видимое окно, чтобы её можно было пройти
        self.auto_visible_on_captcha = auto_visible_on_captcha
        self._switched_to_visible = False
        self.warmup_timeout_sec = warmup_timeout_sec
        # GET выполняем настоящей навигацией: защита отличает переход по ссылке от программного запроса
        self.navigate_for_get = navigate_for_get
        # пауза после загрузки: даём странице дорисовать содержимое скриптом
        self.settle_ms = settle_ms

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._last_request_ts = 0.0
        self.cookies_loaded = 0
        self._recorded: list[dict] = []
        self._response_handler = None
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

    def _handle_captcha(self) -> bool:
        """Реакция на капчу. True -- окно только что открыли, страницу надо перезагрузить.

        В видимом окне просто ждём человека; в скрытом -- один раз переоткрываем браузер с окном,
        а если это запрещено, сообщаем понятной ошибкой.
        """
        if not self.headless:
            return False
        if self.auto_visible_on_captcha and not self._switched_to_visible:
            self._switched_to_visible = True
            log.warning("сайт показывает капчу: открываю видимое окно браузера -- пройдите её там, "
                        "команда продолжит сама (ожидание до %.0f с)", self.captcha_timeout_sec)
            url = self._page.url or self.warmup_url
            self._close_browser()
            self.headless = False
            self._launch(self.user_agent)
            try:
                self._page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                if not _is_navigation_error(exc):
                    raise
            return True
        raise CaptchaRequired(
            "сайт показывает капчу. Запустите ту же команду с видимым окном (--show-browser), пройдите капчу "
            "в нём -- профиль сохранится в каталоге браузера, и дальше команды пойдут без неё")

    def _read_user_agent(self) -> str:
        try:
            return self._page.evaluate("() => navigator.userAgent") or ""
        except Exception as exc:  # noqa: BLE001
            if _is_navigation_error(exc):
                return self.user_agent or ""
            raise

    def _reset_recording(self) -> None:
        self._response_handler = None

    def _close_browser(self) -> None:
        self._reset_recording()
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
        announced_captcha = False
        while time.time() < deadline:
            html = self._safe_content()
            if html is not None and self.captcha_detector(html):
                self._handle_captcha()
                if not announced_captcha:
                    announced_captcha = True
                    deadline = max(deadline, time.time() + self.captcha_timeout_sec)
                    if not self._switched_to_visible:
                        log.warning("сайт показывает капчу: пройдите её в открывшемся окне браузера, "
                                    "команда продолжит сама (ожидание до %.0f с)", self.captcha_timeout_sec)
            elif html is not None and not self.stub_detector(html):
                html = self._settle(html)
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
        self._ensure_started()

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
        announced_captcha = False
        while time.time() < deadline:
            current = self._safe_content()
            if current is not None:
                html = current
                if self.captcha_detector(html):
                    self._handle_captcha()
                    if not announced_captcha:
                        announced_captcha = True
                        deadline = max(deadline, time.time() + self.captcha_timeout_sec)
                        if not self._switched_to_visible:
                            log.warning("капча: пройдите её в окне браузера, команда продолжит сама")
                elif not self.stub_detector(html):
                    return html
            try:
                self._page.wait_for_timeout(500)
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        return html

    def _settle(self, html: str) -> str:
        """Ждёт затишья в сети и короткую паузу: содержимое может дорисовываться скриптом после загрузки."""
        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:  # noqa: BLE001 -- затишья может не быть (опросы, счётчики)
            pass
        if self.settle_ms:
            try:
                self._page.wait_for_timeout(self.settle_ms)
            except Exception:  # noqa: BLE001
                time.sleep(self.settle_ms / 1000)
        return self._safe_content() or html

    def _fetch_by_navigation(self, url: str) -> FetchResult:
        """GET как обычный переход по ссылке: браузер сам проходит проверку и отдаёт готовый DOM."""
        resp = None
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            if not _is_navigation_error(exc):
                raise
        html = self._wait_for_real_content()
        html = self._settle(html)
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

    # ------------------------------------------------------------------ работа со страницей
    def _ensure_started(self) -> None:
        """Браузер поднимается лениво: первая же операция со страницей запускает его."""
        if self._context is None or self._page is None:
            self.start()

    def open_page(self, url: str) -> str:
        """Переход на страницу; возвращает её HTML после прохождения проверок и дорисовки скриптами."""
        self._ensure_started()
        return self._fetch_by_navigation(url).text

    def current_html(self) -> str:
        self._ensure_started()
        return self._settle(self._safe_content() or "")

    def fill_field(self, name: str, value: str, timeout_ms: int = 5000) -> bool:
        """Заполняет поле по атрибуту name. Поля с датой часто readonly -- тогда ставим значение скриптом."""
        self._ensure_started()
        selector = f'input[name="{name}"]'
        try:
            self._page.fill(selector, value, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001 -- readonly/скрытое поле: пробуем через DOM
            try:
                ok = self._page.evaluate(
                    """(arg) => {
                        const el = document.querySelector(`input[name="${arg.name}"]`);
                        if (!el) return false;
                        el.removeAttribute('readonly');
                        el.value = arg.value;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }""", {"name": name, "value": value})
                return bool(ok)
            except Exception:  # noqa: BLE001
                return False

    def set_checkbox_group(self, name: str, values: list[str]) -> int:
        """Отмечает чекбоксы группы с указанными значениями (остальные снимает). Возвращает число отмеченных."""
        self._ensure_started()
        return int(self._page.evaluate(
            """(arg) => {
                const boxes = Array.from(document.querySelectorAll(`input[name="${arg.name}"]`));
                let n = 0;
                for (const b of boxes) {
                    const want = arg.values.includes(b.value);
                    if (b.checked !== want) {
                        b.click();
                        if (b.checked !== want) { b.checked = want; b.dispatchEvent(new Event('change', {bubbles: true})); }
                    }
                    if (want) n++;
                }
                return n;
            }""", {"name": name, "values": [str(v) for v in values]}))

    def click(self, selector: str, timeout_ms: int = 10_000) -> bool:
        """Клик по селектору: обычный, затем принудительный, затем через DOM (элемент может быть перекрыт)."""
        self._ensure_started()
        for kwargs in ({}, {"force": True}):
            try:
                self._page.click(selector, timeout=timeout_ms, **kwargs)
                return True
            except Exception:  # noqa: BLE001
                continue
        return self.js_click(selector)

    def js_click(self, selector: str) -> bool:
        try:
            return bool(self._page.evaluate(
                "(sel) => { const el = document.querySelector(sel); if (!el) return false; el.click(); return true; }",
                selector))
        except Exception:  # noqa: BLE001
            return False

    def click_first(self, selectors: list[str], timeout_ms: int = 4000) -> Optional[str]:
        """Кликает первый существующий селектор из списка; возвращает сработавший."""
        for sel in selectors:
            if self.count(sel) and self.click(sel, timeout_ms):
                return sel
        for sel in selectors:                      # запасной проход: элемент мог появиться позже
            if self.click(sel, timeout_ms):
                return sel
        return None

    def count(self, selector: str) -> int:
        """Сколько элементов подходит под селектор (0 -- селектор не о том)."""
        self._ensure_started()
        try:
            return int(self._page.evaluate("(sel) => document.querySelectorAll(sel).length", selector))
        except Exception:  # noqa: BLE001
            return 0

    def dismiss_overlays(self, selectors: Optional[list[str]] = None) -> list[str]:
        """Закрывает баннеры (согласие на cookies и т.п.), которые перехватывают клики."""
        selectors = selectors or ["#AcceptCookieBtn", "#acceptCookie", ".cookie-accept", "[id*=AcceptCookie]",
                                  "[class*=cookie] button", "[id*=cookie] button"]
        closed = []
        for sel in selectors:
            if self.count(sel) and self.js_click(sel):
                closed.append(sel)
        return closed

    # ------------------------------------------------------------------ запись сетевых обменов
    ANALYTICS_HOSTS = ("mc.yandex.ru", "top100.ru", "top-fwz1.mail.ru", "yandex.ru/ads", "google-analytics",
                       "googletagmanager", "doubleclick", "vk.com", "criteo", "adfox")

    def start_recording(self) -> None:
        """Начинает записывать запросы страницы: так видно, какой адрес вызывает форма."""
        self._ensure_started()
        self._recorded = []
        if self._response_handler is None:
            def _on_response(resp):
                try:
                    req = resp.request
                    self._recorded.append({
                        "url": resp.url, "method": req.method, "status": resp.status,
                        "resource_type": req.resource_type,
                        "content_type": (resp.headers or {}).get("content-type", ""),
                        "post_data": (req.post_data or "")[:1000],
                    })
                except Exception:  # noqa: BLE001 -- запись диагностики не должна ничему мешать
                    pass
            self._response_handler = _on_response
            self._page.on("response", _on_response)

    def recorded_requests(self, skip_analytics: bool = True, types: tuple[str, ...] = ("xhr", "fetch", "document")) -> list[dict]:
        out = []
        for r in getattr(self, "_recorded", []):
            if types and r.get("resource_type") not in types:
                continue
            if skip_analytics and any(h in r["url"] for h in self.ANALYTICS_HOSTS):
                continue
            out.append(r)
        return out

    def list_clickables(self, limit: int = 60) -> list[dict]:
        """Кнопки и ссылки-кнопки на странице -- для диагностики, когда нужный элемент не найден."""
        self._ensure_started()
        try:
            return list(self._page.evaluate(
                """(limit) => Array.from(document.querySelectorAll(
                        'button, input[type=submit], input[type=button], a[role=button], [class*=button], [id*=button]'))
                    .slice(0, limit)
                    .map(e => ({tag: e.tagName.toLowerCase(), id: e.id || null,
                                cls: (e.className && e.className.toString().slice(0, 80)) || null,
                                text: ((e.innerText || e.value || '').trim().slice(0, 50)) || null,
                                visible: !!(e.offsetParent || e.getClientRects().length)}))""", limit))
        except Exception:  # noqa: BLE001
            return []

    def wait_for_selector(self, selector: str, timeout_ms: int = 20_000) -> bool:
        self._ensure_started()
        try:
            self._page.wait_for_selector(selector, timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            return False

    def field_names(self) -> list[str]:
        self._ensure_started()
        try:
            return list(self._page.evaluate(
                "() => Array.from(document.querySelectorAll('input[name],select[name],textarea[name]'))"
                ".map(e => e.name).filter((v, i, a) => a.indexOf(v) === i)"))
        except Exception:  # noqa: BLE001
            return []

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
