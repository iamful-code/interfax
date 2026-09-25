from __future__ import annotations

from datetime import datetime

from disclosure_alpha.edisclosure import parsers
from tests.conftest import read_fixture


def test_parse_search_form_fields_and_event_types():
    spec = parsers.parse_search_form(read_fixture("edisclosure_search_page.html"))
    assert spec is not None
    assert spec.action.endswith("/poisk-po-soobshheniyam")
    assert spec.method == "post"
    assert spec.fields["dateStart"] == "01.01.2024"
    assert spec.fields["lastPageNumber"] == "1"
    assert spec.fields["lastPageSizeSelect"] == "10"
    assert "eventTypeCheckboxGroup" in spec.checkbox_groups
    opts = spec.event_type_options()
    assert opts[0]["group"] == "eventTypeCheckboxGroup"
    by_value = {o["value"]: o for o in opts}
    assert "изменении размера доли" in by_value["52"]["label"]
    assert by_value["77"]["checked"] is True
    assert spec.radio_groups["radView"][0]["checked"] is True


def test_parse_search_results_rows():
    rows = parsers.parse_search_results(read_fixture("edisclosure_search_results.html"))
    assert len(rows) == 3
    r = rows[0]
    assert r.event_id == "pHCdwvYkFkau-A8zAGePZqg-B-B"
    assert r.company_id == 3043
    assert r.company_name == "ПАО Сбербанк"
    assert r.published_at == datetime(2024, 3, 15, 18, 52)
    assert r.event_type.startswith("Об изменении размера доли участия")
    assert r.url == "https://e-disclosure.ru/portal/event.aspx?EventId=pHCdwvYkFkau-A8zAGePZqg-B-B"
    assert rows[2].company_id == 1976 and rows[2].published_at == datetime(2024, 3, 14, 9, 41)
    assert parsers.parse_total_pages(read_fixture("edisclosure_search_results.html")) == 1


def test_parse_search_results_empty():
    assert parsers.parse_search_results(read_fixture("edisclosure_search_results_empty.html")) == []


def test_parse_lastnews_items():
    rows = parsers.parse_lastnews(read_fixture("edisclosure_lastnews.html"))
    assert len(rows) == 2
    assert rows[0].event_id == "HHV9O508sEq2iW7SEtqazg-B-B"
    assert rows[0].published_at == datetime(2026, 9, 24, 10, 15)
    assert rows[0].company_name == 'ПАО "Пример"'
    assert rows[0].company_id is None
    assert "рейтинга" in rows[0].event_type
    assert rows[1].company_name == 'АО "Другой"'


def test_parse_event_page_714():
    ev = parsers.parse_event_page(read_fixture("edisclosure_event_stake_714.html"))
    assert ev.event_id == "pHCdwvYkFkau-A8zAGePZqg-B-B"
    assert ev.company_id == 3043
    assert ev.company_name == "ПАО Сбербанк"
    assert ev.published_at == datetime(2024, 3, 15, 18, 52)
    assert "до изменения" in ev.body_text and "после изменения" in ev.body_text
    assert "© Интерфакс" not in ev.body_text  # footer отрезан
    assert "\n2.4." in ev.body_text  # <br> превращены в переводы строк


def test_parse_event_page_single_line_text():
    ev = parsers.parse_event_page(read_fixture("edisclosure_event_stake_454_sale.html"), "X")
    assert ev.event_id == "X"
    assert ev.company_id == 1976
    assert ev.published_at == datetime(2019, 11, 25, 17, 3)


def test_parse_company_page():
    ci = parsers.parse_company_page(read_fixture("edisclosure_company.html"), 3043)
    assert ci.company_id == 3043
    assert ci.inn == "7707083893"
    assert ci.ogrn == "1027700132195"
    assert ci.okpo == "00032537"
    assert ci.name == "ПАО Сбербанк"
    assert ci.region == "Москва"


def test_find_links_and_script_endpoints():
    html = read_fixture("edisclosure_search_page.html")
    assert "https://e-disclosure.ru/portal/lastnews.aspx" in parsers.find_links(html, "https://e-disclosure.ru", r"/portal/")
    eps = parsers.find_script_endpoints(html)
    assert any(e.startswith("/Event/Page") for e in eps)


def test_parse_datetime_ru_variants():
    assert parsers.parse_datetime_ru("15.03.2024") == datetime(2024, 3, 15)
    assert parsers.parse_datetime_ru("x 15.03.2024 18:52:07 y") == datetime(2024, 3, 15, 18, 52, 7)
    assert parsers.parse_datetime_ru("нет даты") is None
    assert parsers.parse_datetime_ru("31.02.2024") is None


def test_parse_search_json_real_shape():
    """Ответ поиска сайта: список foundEventsList с полями компании, типа и даты."""
    from disclosure_alpha.edisclosure.parsers import parse_search_json, total_from_payload

    payload = {"foundEventsList": [
        {"highlighted": "<b>Изменение</b> размера доли участия", "agency": "ИА АК&М", "companyID": 31793,
         "companyName": 'АО "Радуга"', "eventName": "Изменение размера доли участия члена органа управления",
         "pseudoGUID": "OW2stjkYJ0Cbt5c5XS3ANw-B-B", "eventDate": "2026-03-14T18:52:00"}], "totalCount": 1}
    rows = parse_search_json(payload)
    assert len(rows) == 1
    r = rows[0]
    assert r.event_id == "OW2stjkYJ0Cbt5c5XS3ANw-B-B"
    assert r.company_id == 31793 and r.company_name == 'АО "Радуга"'
    assert r.published_at == datetime(2026, 3, 14, 18, 52)
    assert r.event_type.startswith("Изменение размера доли")
    assert r.url.endswith("EventId=OW2stjkYJ0Cbt5c5XS3ANw-B-B")
    assert total_from_payload(payload) == 1


def test_parse_search_json_tolerates_other_names():
    """Имена полей могут отличаться: ищем по смыслу, дату -- в любом поле."""
    from disclosure_alpha.edisclosure.parsers import parse_search_json

    rows = parse_search_json({"items": [
        {"guid": "ABCdef1234567890xyz", "company_id": "77", "orgName": "ПАО Икс",
         "title": "О созыве общего собрания", "someDate": "24.09.2026 10:15"}]})
    assert len(rows) == 1
    assert rows[0].event_id == "ABCdef1234567890xyz" and rows[0].company_id == 77
    assert rows[0].published_at == datetime(2026, 9, 24, 10, 15)
    assert rows[0].company_name == "ПАО Икс"


def test_parse_search_json_ignores_unusable_rows():
    from disclosure_alpha.edisclosure.parsers import find_event_list, parse_search_json

    assert parse_search_json({"foundEventsList": [{"companyName": "без идентификатора"}]}) == []
    assert parse_search_json({}) == []
    assert find_event_list([{"a": 1}]) == [{"a": 1}]
