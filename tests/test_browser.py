"""Проверка браузерного транспорта на локальном сервере, имитирующем JS-проверку сайта."""
from __future__ import annotations

import threading
from datetime import date, datetime
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
        if urlparse(self.path).path.endswith(".json"):
            data = '{"ok": true, "n": 42}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
            return
        if urlparse(self.path).path == "/poisk-po-soobshheniyam":
            return self._send(r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Поиск</title></head><body>
<form id="f"><input type="text" name="dateStart" readonly><input type="text" name="dateFinish" readonly>
<input type="checkbox" name="eventTypeCheckboxGroup" value="52"><input type="checkbox" name="eventTypeCheckboxGroup" value="12">
<button type="button" id="sEventSearchForm__button-search">Найти</button></form>
<div id="results"></div>
<script>
document.getElementById('sEventSearchForm__button-search').addEventListener('click', function(){
  const types = Array.from(document.querySelectorAll('input[name=eventTypeCheckboxGroup]:checked')).map(b => b.value);
  const from = document.querySelector('input[name=dateStart]').value.replace(/\./g, '');
  const till = document.querySelector('input[name=dateFinish]').value.replace(/\./g, '');
  setTimeout(function(){
    document.getElementById('results').innerHTML =
      '<table><tr><td>15.03.2024 18:52</td><td><a href="/portal/company.aspx?id=3043">ПАО Сбербанк</a></td>' +
      '<td><a href="/portal/event.aspx?EventId=E-' + from + '-' + till + '-' + types.join('_') + '">Тип сообщения</a></td></tr></table>';
  }, 300);
});
</script></body></html>""")
        if urlparse(self.path).path == "/spa":
            return self._send("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>SPA</title></head>
<body><div id="app"></div><script>setTimeout(function(){
  document.getElementById('app').innerHTML =
    '<form action="/poisk" method="post"><input type="text" name="dateStart"><input type="checkbox" name="eventTypeCheckboxGroup" value="52"></form>';
}, 400);</script></body></html>""")
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


def test_recovers_when_cookies_dropped(transport, server):
    """После сброса cookies навигация снова проходит проверку (страница перезагружает себя сама)."""
    base = f"http://127.0.0.1:{server.server_address[1]}"
    transport._context.clear_cookies()
    res = transport.get(base + "/poisk", use_cache=False)
    assert "Настоящая страница" in res.text
    assert not _stub(res.text)


def test_get_uses_real_navigation(transport, server):
    """GET должен быть настоящим переходом: защита отличает его от программного запроса."""
    base = f"http://127.0.0.1:{server.server_address[1]}"
    before = len(server.hits)
    transport.get(base + "/poisk", use_cache=False)
    navigations = [h for h in server.hits[before:] if h[0] == "GET"]
    assert navigations, "переход не выполнен"
    assert transport._page.url.startswith(base)        # страница действительно перешла по адресу


def test_json_request_bypasses_dom(transport, server):
    """get_json не должен заворачивать ответ в DOM-обёртку браузера."""
    base = f"http://127.0.0.1:{server.server_address[1]}"
    data = transport.get_json(base + "/api.json", use_cache=False)
    assert data == {"ok": True, "n": 42}


class _FlakyPage:
    """Страница, которая при первом обращении «перезагружается» (как во время JS-проверки)."""

    def __init__(self, error_cls, html="<html><body>ok</body></html>"):
        self.calls = 0
        self.error_cls = error_cls
        self.html = html

    def wait_for_load_state(self, *a, **kw):
        return None

    def wait_for_timeout(self, *a, **kw):
        return None

    def content(self):
        self.calls += 1
        if self.calls == 1:
            raise self.error_cls("Page.content: Unable to retrieve content because the page is navigating "
                                 "and changing the content.")
        return self.html

    def evaluate(self, *a, **kw):
        raise self.error_cls("Execution context was destroyed, most likely because of a navigation.")


def test_safe_content_survives_navigation(server):
    from playwright.sync_api import Error as PwError

    from disclosure_alpha.browser import _is_navigation_error, normal_user_agent

    tr = BrowserTransport(warmup_url="http://127.0.0.1:1/", stub_detector=_stub)
    tr._page = _FlakyPage(PwError)
    assert tr._safe_content() is None          # первая попытка приходится на перезагрузку
    assert tr._safe_content() == "<html><body>ok</body></html>"
    tr.user_agent = "UA"
    assert tr._read_user_agent() == "UA"        # ошибка навигации не валит чтение User-Agent

    assert _is_navigation_error(PwError("Page.content: ... navigating and changing the content."))
    assert not _is_navigation_error(PwError("net::ERR_CONNECTION_REFUSED"))
    assert normal_user_agent("Mozilla/5.0 HeadlessChrome/141") == "Mozilla/5.0 Chrome/141"


def test_safe_content_reraises_real_errors():
    from playwright.sync_api import Error as PwError

    class _Broken(_FlakyPage):
        def content(self):
            raise self.error_cls("net::ERR_CONNECTION_REFUSED")

    tr = BrowserTransport(warmup_url="http://127.0.0.1:1/", stub_detector=_stub)
    tr._page = _Broken(PwError)
    with pytest.raises(PwError):
        tr._safe_content()


def test_navigation_captures_js_rendered_content(transport, server):
    """Содержимое, дорисованное скриптом после загрузки, тоже попадает в результат."""
    base = f"http://127.0.0.1:{server.server_address[1]}"
    res = transport.get(base + "/spa", use_cache=False)
    assert 'name="dateStart"' in res.text          # формы не было в исходном HTML
    from disclosure_alpha.edisclosure.parsers import parse_search_form

    spec = parse_search_form(res.text, base)
    assert spec is not None and "dateStart" in spec.fields
    assert "eventTypeCheckboxGroup" in spec.checkbox_groups


CAPTCHA_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="./sp_rotated_captcha/js/captchaIntGen.js"></script>
<script src="./sp_rotated_captcha/js/bundle.js"></script></head><body><div id="captcha"></div></body></html>"""


class _ScriptedPage:
    """Страница, отдающая заранее заданную последовательность HTML."""

    def __init__(self, pages, url="http://127.0.0.1:1/"):
        self.pages = list(pages)
        self.reads = 0
        self.url = url

    def goto(self, url, **kw):
        self.url = url
        return None

    def wait_for_load_state(self, *a, **kw):
        return None

    def wait_for_timeout(self, *a, **kw):
        return None

    def content(self):
        self.reads += 1
        return self.pages[min(self.reads - 1, len(self.pages) - 1)]


class _FakeContext:
    def cookies(self):
        return [{"name": "spsc"}, {"name": "spid"}]


def _captcha_transport(headless: bool, pages):
    from disclosure_alpha.edisclosure.client import is_captcha_page, is_protection_stub

    tr = BrowserTransport(warmup_url="http://127.0.0.1:1/", headless=headless, min_interval_sec=0,
                          stub_detector=is_protection_stub, captcha_detector=is_captcha_page,
                          warmup_timeout_sec=5, captcha_timeout_sec=5)
    tr._page = _ScriptedPage(pages)
    tr._context = _FakeContext()
    return tr


def test_captcha_in_visible_window_waits_until_solved():
    """В видимом окне капчу проходит человек: ждём, пока страница станет настоящей."""
    real = "<html><body><h1>Настоящая страница</h1><a href='/x'>ссылка</a></body></html>"
    tr = _captcha_transport(headless=False, pages=[CAPTCHA_HTML, CAPTCHA_HTML, real])
    html = tr._wait_for_real_content()
    assert "Настоящая страница" in html


def test_captcha_page_classified_and_not_mistaken_for_content():
    from disclosure_alpha.edisclosure.client import classify_page, is_protection_stub

    assert classify_page(CAPTCHA_HTML) == "captcha"
    assert is_protection_stub(CAPTCHA_HTML)          # как контент такую страницу не отдаём
    assert classify_page(REAL) == "content"
    assert classify_page(STUB) == "stub"


def test_search_via_form_fills_and_clicks(transport, server, tmp_path):
    """Поиск выполняется заполнением формы и нажатием кнопки: так работает сайт с клиентским скриптом."""
    from disclosure_alpha.config import Settings
    from disclosure_alpha.edisclosure.client import EDisclosureClient

    base = f"http://127.0.0.1:{server.server_address[1]}"
    settings = Settings(data_dir=tmp_path / "data", edisclosure_base_url=base)
    client = EDisclosureClient(settings, http=transport)
    assert client.is_browser

    rows, html = client.search_via_form(date(2024, 3, 14), date(2024, 3, 15), event_type_ids=["52", "12"])
    assert len(rows) == 1
    r = rows[0]
    assert r.company_id == 3043 and r.company_name == "ПАО Сбербанк"
    assert r.published_at == datetime(2024, 3, 15, 18, 52)
    # значения дошли до формы: даты в формате ДД.ММ.ГГГГ и оба типа сообщений
    assert r.event_id == "E-14032024-15032024-52_12"

    summary = client.probe_search(date(2024, 3, 14), date(2024, 3, 15), ["52"], tmp_path / "probe")
    assert summary["rows_parsed"] == 1
    assert "dateStart" in summary["field_names"]
    assert (tmp_path / "probe" / "search_probe.json").exists()


def test_page_actions_start_browser_lazily(monkeypatch):
    """Операции со страницей поднимают браузер сами: probe-search падал, пока это делал только request()."""
    tr = BrowserTransport(warmup_url="http://127.0.0.1:1/", stub_detector=_stub)
    started = {"n": 0}

    def fake_start():
        started["n"] += 1
        tr._context = _FakeContext()
        tr._page = _ScriptedPage(["<html><body><h1>ok</h1></body></html>"])
        return tr

    monkeypatch.setattr(tr, "start", fake_start)
    assert tr._page is None
    assert tr.current_html() == "<html><body><h1>ok</h1></body></html>"
    assert started["n"] == 1
    tr.field_names()           # второй вызов не перезапускает браузер
    assert started["n"] == 1


def test_captcha_opens_visible_window_automatically(monkeypatch):
    """В скрытом режиме капча приводит к переоткрытию браузера с видимым окном, а не к ошибке."""
    tr = _captcha_transport(headless=True, pages=[CAPTCHA_HTML])
    tr.auto_visible_on_captcha = True
    real = "<html><body><h1>Настоящая страница</h1><a href='/x'>с</a></body></html>"
    relaunched = {"n": 0}

    monkeypatch.setattr(tr, "_close_browser", lambda: None)

    def fake_launch(_ua=None):
        relaunched["n"] += 1
        tr._page = _ScriptedPage([real])
        tr._context = _FakeContext()

    monkeypatch.setattr(tr, "_launch", fake_launch)
    html = tr._wait_for_real_content()
    assert "Настоящая страница" in html
    assert relaunched["n"] == 1 and tr.headless is False and tr._switched_to_visible


def test_captcha_raises_when_auto_visible_disabled():
    from disclosure_alpha.browser import CaptchaRequired

    tr = _captcha_transport(headless=True, pages=[CAPTCHA_HTML])
    tr.auto_visible_on_captcha = False
    with pytest.raises(CaptchaRequired) as e:
        tr._wait_for_real_content()
    assert "--show-browser" in str(e.value)
