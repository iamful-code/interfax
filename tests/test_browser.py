"""Проверка браузерного транспорта на локальном сервере, имитирующем JS-проверку сайта."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from disclosure_alpha.browser import BrowserTransport, BrowserUnavailable, find_chromium

playwright = pytest.importorskip("playwright.sync_api", reason="Playwright не установлен")

STUB = """<!DOCTYPE html><html><head><meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<noscript><meta http-equiv="refresh" content="0; url=/no-js"></noscript></head>
<body><div id="id_spinner" class="spinner-container">проверка</div>
<div id="id_captcha_frame_div"></div>
<script>document.cookie = "spsc=passed; path=/"; setTimeout(function(){ location.reload(); }, 150);</script>
</body></html>"""

REAL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Поиск</title></head>
<body><h1>Настоящая страница</h1><a href="/portal/event.aspx?EventId=E1">событие</a>
<form action="/poisk" method="post"><input type="text" name="dateStart" value="01.01.2024"></form></body></html>"""

RESULTS = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><div id="res">{payload}</div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def _passed(self) -> bool:
        return "spsc=passed" in (self.headers.get("Cookie") or "")

    def _send(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self.server.hits.append(("GET", self.path, self._passed()))
        if not self._passed():
            return self._send(STUB)
        q = parse_qs(urlparse(self.path).query)
        if "EventId" in q:
            return self._send(f"<html><body>событие {q['EventId'][0]}</body></html>")
        self._send(REAL)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        self.server.hits.append(("POST", self.path, self._passed()))
        if not self._passed():
            return self._send(STUB)
        fields = parse_qs(body, keep_blank_values=True)
        summary = ";".join(f"{k}={'|'.join(v)}" for k, v in sorted(fields.items()))
        self._send(RESULTS.format(payload=summary))

    def log_message(self, *a):  # тишина в выводе тестов
        pass


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.hits = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _stub(html: str) -> bool:
    from disclosure_alpha.edisclosure.client import is_protection_stub

    return is_protection_stub(html)


@pytest.fixture(scope="module")
def transport(server, tmp_path_factory):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    tr = BrowserTransport(warmup_url=base + "/poisk", min_interval_sec=0, stub_detector=_stub,
                          cache_dir=tmp_path_factory.mktemp("cache"), warmup_timeout_sec=30,
                          executable_path=find_chromium())
    try:
        tr.start()
    except BrowserUnavailable as e:
        pytest.skip(f"браузер недоступен: {e}")
    yield tr
    tr.close()


def test_warmup_passes_js_check(transport, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    assert transport.cookies_loaded >= 1
    assert transport.session.headers["User-Agent"].startswith("Mozilla/")
    res = transport.get(base + "/poisk", use_cache=False)
    assert res.ok and "Настоящая страница" in res.text
    assert not _stub(res.text)
    assert any(passed for _, _, passed in server.hits)


def test_get_with_params_and_cache(transport, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    res = transport.get(base + "/portal/event.aspx", params={"EventId": "E1"})
    assert "событие E1" in res.text and not res.from_cache
    again = transport.get(base + "/portal/event.aspx", params={"EventId": "E1"})
    assert again.from_cache and "событие E1" in again.text


def test_post_form_with_repeated_fields(transport, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    res = transport.post(base + "/poisk", data={"dateStart": "01.01.2024", "eventTypeCheckboxGroup": ["52", "12"]})
    assert "dateStart=01.01.2024" in res.text
    assert "eventTypeCheckboxGroup=52|12" in res.text      # повторяющиеся поля дошли списком


def test_rewarms_when_cookies_dropped(transport, server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    transport._context.clear_cookies()
    warmups_before = transport.stats["warmups"]
    res = transport.get(base + "/poisk", use_cache=False)
    assert "Настоящая страница" in res.text
    assert transport.stats["warmups"] > warmups_before   # заглушка распознана, прогрев повторён
