"""Общий HTTP-клиент: лимит частоты запросов, повторы с backoff, дисковый кеш.

Используется и для e-disclosure.ru (HTML), и для MOEX ISS (JSON).
Кеш: ключ = sha1(method + url + params + data); файлы <cache_dir>/<2 символа>/<hash>.bin + .json (мета).
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Заголовки «как у браузера»: часть защит отсекает клиентов с минимальным набором заголовков.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def decode_body(content: bytes, encoding: Optional[str]) -> str:
    """Декодирование ответа: объявленная кодировка, затем utf-8; latin-1 (догадка requests) -- в последнюю очередь."""
    declared = (encoding or "").lower()
    order: list[str] = []
    if declared and declared not in ("iso-8859-1", "latin-1", "latin1"):
        order.append(declared)
    order.append("utf-8")
    for enc in order:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("cp1251", errors="replace") if b"\xd0" not in content[:2000] else content.decode("utf-8", errors="replace")


def load_cookies(session: requests.Session, path: Path, default_domain: str = "e-disclosure.ru") -> int:
    """Загружает cookies из файла: формат Netscape (cookies.txt) или одна строка заголовка Cookie.

    Возвращает число загруженных cookies. Строка вида ``a=1; b=2`` (например, скопированная из DevTools)
    привязывается к default_domain и его поддоменам.
    """
    path = Path(path)
    text = path.read_text("utf-8", errors="replace").strip()
    if not text:
        return 0
    if text.startswith("# Netscape") or text.startswith("# HTTP Cookie File") or "\t" in text.splitlines()[0]:
        jar = http.cookiejar.MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
        n = 0
        for c in jar:
            session.cookies.set_cookie(c)
            n += 1
        return n
    n = 0
    for part in text.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if not name:
            continue
        session.cookies.set(name.strip(), value.strip(), domain="." + default_domain.lstrip("."), path="/")
        n += 1
    return n


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict
    from_cache: bool = False
    encoding: Optional[str] = None

    @property
    def text(self) -> str:
        return decode_body(self.content, self.encoding)

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, message: str = "", body: str = "", headers: Optional[Mapping[str, str]] = None):
        super().__init__(f"HTTP {status} for {url} {message}".strip())
        self.url = url
        self.status = status
        self.body = body            # полное тело ответа (для диагностики защиты/техработ)
        self.headers = dict(headers or {})


def _cache_key(method: str, url: str, params: Optional[Mapping], data: Optional[Mapping]) -> str:
    def _norm(m: Optional[Mapping]) -> str:
        if not m:
            return ""
        items = []
        for k in sorted(m):
            v = m[k]
            if isinstance(v, (list, tuple)):
                v = "|".join(str(x) for x in v)
            items.append(f"{k}={v}")
        return "&".join(items)

    raw = f"{method.upper()} {url}?{_norm(params)}#{_norm(data)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class HttpClient:
    """Синхронный клиент поверх requests.Session.

    Параметры:
        min_interval_sec -- минимальная пауза между запросами (вежливость к серверу);
        cache_dir        -- каталог дискового кеша (None = без кеша);
        cache_ttl_sec    -- срок годности кеша (None = бессрочно);
        session          -- можно подставить свою requests.Session (тесты, прокси, cookies).
    """

    def __init__(
        self,
        min_interval_sec: float = 1.0,
        user_agent: str = "disclosure-alpha/0.1",
        timeout_sec: float = 60.0,
        max_retries: int = 4,
        cache_dir: Optional[Path] = None,
        cache_ttl_sec: Optional[float] = None,
        session: Optional[requests.Session] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        cookies_file: Optional[Path] = None,
        browser_headers: bool = True,
    ):
        self.min_interval_sec = min_interval_sec
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl_sec = cache_ttl_sec
        self.session = session or requests.Session()
        if browser_headers:
            self.session.headers.update(BROWSER_HEADERS)
        self.session.headers.update({"User-Agent": user_agent})
        if extra_headers:
            self.session.headers.update(dict(extra_headers))
        self.cookies_loaded = 0
        if cookies_file and Path(cookies_file).exists():
            try:
                self.cookies_loaded = load_cookies(self.session, Path(cookies_file))
                log.info("загружено cookies из %s: %d", cookies_file, self.cookies_loaded)
            except Exception as exc:  # noqa: BLE001
                log.warning("не удалось загрузить cookies из %s: %s", cookies_file, exc)
        self._last_request_ts = 0.0
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0}

    # ------------------------------------------------------------------ cache
    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        assert self.cache_dir is not None
        d = self.cache_dir / key[:2]
        return d / f"{key}.bin", d / f"{key}.json"

    def _cache_get(self, key: str) -> Optional[FetchResult]:
        if not self.cache_dir:
            return None
        bin_path, meta_path = self._cache_paths(key)
        if not (bin_path.exists() and meta_path.exists()):
            return None
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if self.cache_ttl_sec is not None and time.time() - meta.get("fetched_at", 0) > self.cache_ttl_sec:
            return None
        return FetchResult(
            url=meta.get("url", ""), status=int(meta.get("status", 200)), content=bin_path.read_bytes(),
            headers=meta.get("headers", {}), from_cache=True, encoding=meta.get("encoding"),
        )

    def _cache_put(self, key: str, res: FetchResult) -> None:
        if not self.cache_dir or not res.ok:
            return
        bin_path, meta_path = self._cache_paths(key)
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_bytes(res.content)
        meta_path.write_text(json.dumps({
            "url": res.url, "status": res.status, "headers": res.headers,
            "encoding": res.encoding, "fetched_at": time.time(),
        }, ensure_ascii=False), "utf-8")

    # ---------------------------------------------------------------- request
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
    ) -> FetchResult:
        key = _cache_key(method, url, params, data)
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self._last_request_ts = time.time()
            self.stats["requests"] += 1
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, headers=headers,
                    timeout=self.timeout_sec, allow_redirects=allow_redirects,
                )
            except requests.RequestException as exc:  # сеть/таймаут
                last_exc = exc
                log.warning("request error %s %s: %s (attempt %d)", method, url, exc, attempt + 1)
                self._sleep_backoff(attempt)
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self.stats["retries"] += 1
                retry_after = resp.headers.get("Retry-After")
                log.warning("HTTP %s for %s, retry (attempt %d)", resp.status_code, url, attempt + 1)
                self._sleep_backoff(attempt, retry_after)
                continue

            res = FetchResult(
                url=str(resp.url), status=resp.status_code, content=resp.content,
                headers={k: v for k, v in resp.headers.items()}, encoding=resp.encoding,
            )
            if not res.ok:
                body = res.text
                raise HttpError(url, res.status, body[:120].replace("\n", " ").strip(), body=body, headers=res.headers)
            if use_cache:
                self._cache_put(key, res)
            return res

        raise HttpError(url, -1, f"exhausted retries: {last_exc}")

    def _sleep_backoff(self, attempt: int, retry_after: Optional[str] = None) -> None:
        delay = 2.0 ** attempt
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(min(delay, 60.0))

    def get(self, url: str, params: Optional[Mapping] = None, **kw) -> FetchResult:
        return self.request("GET", url, params=params, **kw)

    def post(self, url: str, data: Optional[Mapping] = None, params: Optional[Mapping] = None, **kw) -> FetchResult:
        kw.setdefault("use_cache", False)
        return self.request("POST", url, params=params, data=data, **kw)

    def get_json(self, url: str, params: Optional[Mapping] = None, **kw) -> Any:
        return self.get(url, params=params, **kw).json()

    def get_text(self, url: str, params: Optional[Mapping] = None, **kw) -> str:
        return self.get(url, params=params, **kw).text
