"""Trading status inferred from collected quote days."""

from datetime import date

import pytest
from test_fsc_adjustments import quote, write_day

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.krx.fsc_states import status_events

A, B = "KR7000001000", "KR7000002000"
DAYS = [date(2024, 6, d) for d in (3, 4, 5, 7, 10)]


def row(isin, code, *, halted=False, market="KOSDAQ"):
    item = quote(
        isin,
        1000,
        0,
        10,
        code=code,
        opening="0" if halted else None,
        volume="0" if halted else "100",
    )
    item["mrktCtg"] = market
    return item


def test_halts_gaps_delisting_and_market_moves(tmp_path):
    write_day(tmp_path, "2024-06-03", [row(A, "000001"), row(B, "000002")])
    write_day(tmp_path, "2024-06-04", [row(A, "000001", halted=True), row(B, "000002")])
    write_day(tmp_path, "2024-06-05", [row(B, "000002")])
    write_day(tmp_path, "2024-06-07", [row(A, "000001")])
    write_day(tmp_path, "2024-06-10", [row(A, "000001", market="KOSPI")])
    events = status_events(tmp_path, tuple(DAYS))
    seen = [(e.symbol, e.session.day, e.status, e.market) for e in events]
    assert seen == [
        ("000001", 3, "NORMAL", "KOSDAQ"),
        ("000001", 4, "SUSPENDED", "KOSDAQ"),  # no-trade row
        # 06-05 missing but back on 06-07: still SUSPENDED, no new event
        ("000001", 7, "NORMAL", "KOSDAQ"),
        ("000001", 10, "NORMAL", "KOSPI"),  # moved market
        ("000002", 3, "NORMAL", "KOSDAQ"),
        ("000002", 7, "DELISTED", "KOSDAQ"),  # never returns
    ]
    first = events[0]
    assert first.effective_at == KRX.open_at(date(2024, 6, 3))
    assert first.available_at == KRX.historical_bar_at(date(2024, 6, 3))


def test_unknown_market_and_uncollected_session_are_refused(tmp_path):
    write_day(tmp_path, "2024-06-03", [row(A, "000001", market="")])
    with pytest.raises(SpecUndefinedError, match="unknown market"):
        status_events(tmp_path, (date(2024, 6, 3),))
    with pytest.raises(SpecUndefinedError, match="not collected"):
        status_events(tmp_path, (date(2024, 6, 4),))
