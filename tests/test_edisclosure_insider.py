from __future__ import annotations

from datetime import date

import pytest

from disclosure_alpha.edisclosure import parsers
from disclosure_alpha.edisclosure.insider import classify_role, is_stake_change_text, parse_number, parse_stake_change
from tests.conftest import read_fixture


def _body(name: str) -> str:
    return parsers.parse_event_page(read_fixture(name)).body_text


def test_parse_number():
    assert parse_number("0,0012 %") == pytest.approx(0.0012)
    assert parse_number("0%") == 0.0
    assert parse_number("1 234,5") == pytest.approx(1234.5)
    assert parse_number("12.75%") == pytest.approx(12.75)
    assert parse_number("не применимо") is None
    assert parse_number(None) is None


def test_stake_change_buy_714():
    body = _body("edisclosure_event_stake_714.html")
    assert is_stake_change_text(body)
    sc = parse_stake_change(body, "E1")
    assert sc.event_id == "E1"
    assert sc.person == "Иванов Иван Иванович"
    assert sc.position.startswith("Член Правления")
    assert sc.is_subsidiary is False and sc.organization is None
    assert sc.share_before == pytest.approx(0.0012)
    assert sc.share_after == pytest.approx(0.0025)
    assert sc.common_before == pytest.approx(0.0015)
    assert sc.common_after == pytest.approx(0.0031)
    assert sc.change_date == date(2024, 3, 15)
    assert sc.direction == 1
    assert sc.delta_pp == pytest.approx(0.0013)
    assert classify_role(sc.position) == "management_board"


def test_stake_change_sale_single_line_454():
    body = _body("edisclosure_event_stake_454_sale.html")
    sc = parse_stake_change(body, "E2")
    assert sc.person == "Сидоров Сидор Сидорович"
    assert sc.position == "Член Совета директоров"
    assert sc.share_before == pytest.approx(0.0104)
    assert sc.share_after == 0.0
    assert sc.common_before == pytest.approx(0.0121)
    assert sc.common_after == 0.0
    assert sc.direction == -1
    assert sc.change_date == date(2019, 11, 22)
    # организация в п.2.3 -- сам эмитент, а не подконтрольная: признак подконтрольности не ставим
    assert sc.is_subsidiary is False
    assert classify_role(sc.position) == "board"


def test_stake_change_subsidiary():
    body = _body("edisclosure_event_stake_subsidiary.html")
    sc = parse_stake_change(body, "E3")
    assert sc.is_subsidiary is True
    assert sc.organization.startswith("Акционерное общество «Дочка»")
    assert sc.share_before == pytest.approx(1.5) and sc.share_after == pytest.approx(2.0)
    assert sc.direction == 1
    assert sc.change_date == date(2022, 2, 1)
    assert classify_role(sc.position) == "ceo"


def test_stake_change_missing_fields():
    sc = parse_stake_change("Какой-то текст без нужных полей", "E4")
    assert sc.direction == 0 and sc.delta_pp is None and sc.person is None
    assert not is_stake_change_text("Какой-то текст")


def test_classify_role():
    assert classify_role("Председатель Совета директоров") == "board_chair"
    assert classify_role("Генеральный директор") == "ceo"
    assert classify_role("Член Совета директоров") == "board"
    assert classify_role("Вице-президент") == "management_board"
    assert classify_role("Главный бухгалтер") == "other"
    assert classify_role(None) == "unknown"
