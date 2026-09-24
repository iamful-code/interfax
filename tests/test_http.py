from __future__ import annotations

from disclosure_alpha.http import BROWSER_HEADERS, HttpClient, HttpError, decode_body, load_cookies
from tests.conftest import FakeSession


def test_decode_body_prefers_utf8_over_latin1_guess():
    raw = "Интерфакс – Сервер раскрытия информации".encode("utf-8")
    assert decode_body(raw, "ISO-8859-1") == "Интерфакс – Сервер раскрытия информации"
    assert decode_body("привет".encode("cp1251"), "windows-1251") == "привет"
    assert decode_body(raw, None) == "Интерфакс – Сервер раскрытия информации"


def test_http_error_carries_body_and_headers():
    rules = [(lambda m, url, p, d: True, lambda *a: (503, "<html><title>Интерфакс</title><script>document.cookie='x=1'</script></html>"))]
    session = FakeSession(rules)
    client = HttpClient(min_interval_sec=0, max_retries=0, session=session)
    try:
        client.get("https://e-disclosure.ru/poisk-po-soobshheniyam")
        raise AssertionError("ожидалась HttpError")
    except HttpError as e:
        assert e.status == 503
        assert "document.cookie" in e.body
        assert "Content-Type" in e.headers
    assert session.headers["User-Agent"] == "disclosure-alpha/0.1"
    assert session.headers["Accept"] == BROWSER_HEADERS["Accept"]


def test_load_cookies_netscape_and_header_formats(tmp_path):
    import requests

    netscape = tmp_path / "cookies.txt"
    netscape.write_text("# Netscape HTTP Cookie File\n.e-disclosure.ru\tTRUE\t/\tTRUE\t2000000000\t__ddg1_\tabc\n"
                        "e-disclosure.ru\tFALSE\t/\tFALSE\t0\tASP.NET_SessionId\txyz\n", "utf-8")
    s = requests.Session()
    assert load_cookies(s, netscape) == 2
    assert s.cookies.get("__ddg1_") == "abc" and s.cookies.get("ASP.NET_SessionId") == "xyz"

    header = tmp_path / "cookie_header.txt"
    header.write_text("a=1; b=2=3; ; c=", "utf-8")
    s2 = requests.Session()
    assert load_cookies(s2, header) == 3
    assert s2.cookies.get("b") == "2=3"

    client = HttpClient(min_interval_sec=0, session=FakeSession([]), cookies_file=netscape)
    assert client.cookies_loaded == 2
