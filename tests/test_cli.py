from __future__ import annotations

import pytest

from disclosure_alpha.cli import build_parser


def test_parser_subcommands_and_defaults():
    p = build_parser()
    a = p.parse_args(["messages", "--from", "2024-01-01", "--till", "2024-02-01", "--categories", "insider_stake_change,buyback"])
    assert a.cmd == "messages" and a.date_from == "2024-01-01" and a.chunk_days == 1
    a = p.parse_args(["backtest", "--hold-days", "40", "--direction", "-1"])
    assert a.hold_days == 40 and a.direction == -1 and a.categories == "insider_stake_change"
    a = p.parse_args(["study", "--model", "market_adjusted"])
    assert a.model == "market_adjusted" and a.bootstrap == 2000
    with pytest.raises(SystemExit):
        p.parse_args(["study", "--model", "bogus"])
    with pytest.raises(SystemExit):
        p.parse_args([])
