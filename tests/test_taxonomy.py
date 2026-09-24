from __future__ import annotations

import pytest

from disclosure_alpha.edisclosure.taxonomy import default_taxonomy


@pytest.mark.parametrize("title,expected", [
    ("Об изменении размера доли участия лиц, входящих в состав органов управления эмитента, в уставном капитале эмитента", "insider_stake_change"),
    ("Изменение размера доли участия члена органа управления эмитента в его уставном капитале", "insider_stake_change"),
    ("О приобретении эмитентом собственных акций", "buyback"),
    ("О рекомендациях в отношении размеров дивидендов по акциям эмитента и порядка их выплаты", "dividends_recommendation"),
    ("О начисленных доходах по эмиссионным ценным бумагам эмитента", "dividends_decision"),
    ("О приобретении лицом голосующих акций эмитента", "major_holder_change"),
    ("О проведении заседания совета директоров эмитента и его повестке дня", "board_agenda"),
    ("Об отдельных решениях, принятых советом директоров эмитента", "board_decisions"),
    ("О созыве общего собрания участников (акционеров) эмитента", "agm_call"),
    ("О проведении общего собрания участников (акционеров) эмитента и о принятых им решениях", "agm_results"),
    ("О совершении эмитентом существенной сделки", "material_transaction"),
    ("О раскрытии эмитентом консолидированной финансовой отчетности", "financial_statements"),
    ("Присвоение или изменение рейтинга эмитента", "rating"),
    ("О неисполнении обязательств эмитента перед владельцами его эмиссионных ценных бумаг", "default_or_debt"),
    ("Об иных событиях (действиях), оказывающих, по мнению эмитента, существенное влияние на стоимость его эмиссионных ценных бумаг", "significant_other"),
    ("Сообщение об изменении или корректировке информации, ранее опубликованной в Ленте новостей", "correction"),
    ("Сведения о сделках инсайдеров", "insider_transactions"),
    ("Что-то непонятное", "other"),
    ("", "other"),
])
def test_classify(title, expected):
    assert default_taxonomy().classify_name(title) == expected


def test_static_direction():
    tx = default_taxonomy()
    assert tx.static_direction("buyback") == 1
    assert tx.static_direction("default_or_debt") == -1
    assert tx.static_direction("board_agenda") == 0
    assert tx.by_name["insider_stake_change"].direction_rule == "stake_change"
