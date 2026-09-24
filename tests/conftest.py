from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text("utf-8")


class FakeResponse:
    def __init__(self, status_code: int, content: bytes, url: str, headers: dict | None = None):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class FakeSession:
    """Подмена requests.Session: маршрутизация по URL/параметрам через список правил.

    rules: список кортежей (predicate(method, url, params, data) -> bool, responder(...) -> (status, body_str)).
    """

    def __init__(self, rules):
        self.headers: dict = {}
        self.rules = rules
        self.calls: list[dict] = []

    def request(self, method, url, params=None, data=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "params": params, "data": data, "headers": headers})
        for pred, responder in self.rules:
            if pred(method, url, params or {}, data or {}):
                status, body = responder(method, url, params or {}, data or {})
                return FakeResponse(status, body.encode("utf-8"), url)
        return FakeResponse(404, b"not found", url)
