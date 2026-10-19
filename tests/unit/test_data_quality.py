from __future__ import annotations

import csv
import json
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import Bar, session_time
from trading_agent.data import CSV_FIELDS, Snapshot
from trading_agent.selection import SecurityHistory, SecurityState

from momentum.agent.contracts import AnnouncedAction, ResearchInput
from momentum.agent.quality import (
    QUARANTINE_CODES,
    assess,
    quarantine,
    screen_price_rows,
)
from momentum.ledger.ledger import CorporateAction


def sessions(count: int, start: date = date(2024, 1, 2)) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def row(day: date, symbol: str = "AAA", close: str = "1000", **changes) -> dict:
    base = {
        "session": day.isoformat(),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": "1000",
        "available_at": session_time(day, 15, 40).isoformat(),
        "source": "TEST",
    }
    base.update(changes)
    return base


def bar(day: date, symbol: str = "AAA", close: str = "1000", volume: int = 1000) -> Bar:
    price = D(close)
    return Bar(day, symbol, price, price, price, price, volume, session_time(day, 15, 40), "TEST")


def state(day: date, symbol: str = "AAA", status: str = "NORMAL") -> SecurityState:
    return SecurityState(
        symbol,
        session_time(day, 15, 30),
        session_time(day, 15, 40),
        status,
        "STOCK",
        "KOSPI",
        False,
        False,
        False,
        False,
        None,
        None,
        "TEST",
    )


def research(bars, states, days, actions=()) -> ResearchInput:
    return ResearchInput(
        Snapshot(bars, require_complete=False),
        SecurityHistory(states),
        tuple(actions),
        tuple(days),
    )


def codes(report, symbol=None, day=None):
    return {
        i.code
        for i in report.issues
        if (symbol is None or i.symbol == symbol) and (day is None or i.session == day)
    }


def test_row_screen_quarantines_each_invalid_row_without_dropping_the_file():
    d = sessions(8)
    rows = [
        row(d[0]),
        row(d[1], open="0"),
        row(d[2], high="900", low="900"),
        row(d[3], volume="-1"),
        row(d[4], available_at=session_time(d[4], 15, 0).isoformat()),
        row(d[5], close="abc"),
        row(d[6]),
        row(d[6], close="1001", high="1001", low="1000", open="1000"),
        row(d[7]),
    ]
    result = screen_price_rows(rows)
    assert [r["session"] for r in result.accepted] == [d[0].isoformat(), d[7].isoformat()]
    by_line = {q.line: q.codes for q in result.quarantined}
    assert by_line == {
        3: ("NONPOSITIVE_PRICE",),
        4: ("OHLC_INCONSISTENT",),
        5: ("NEGATIVE_VOLUME",),
        6: ("AVAILABLE_BEFORE_CLOSE",),
        7: ("PARSE_ERROR",),
        8: ("DUPLICATE_ROW",),
        9: ("DUPLICATE_ROW",),
    }
    # The original row is preserved verbatim for audit.
    assert result.quarantined[0].row == rows[1]
    for accepted in result.accepted:
        Bar(
            date.fromisoformat(accepted["session"]),
            accepted["symbol"],
            D(accepted["open"]),
            D(accepted["high"]),
            D(accepted["low"]),
            D(accepted["close"]),
            int(accepted["volume"]),
            session_time(date.fromisoformat(accepted["session"]), 15, 40),
        )


def test_abnormal_return_is_quarantined_but_a_same_day_split_explains_it():
    d = sessions(4)
    bars = [bar(d[0], close="1000"), bar(d[1], close="1400"), bar(d[2], close="1400")]
    bars += [bar(d[0], "BBB", "1000"), bar(d[1], "BBB", "500")]
    split = AnnouncedAction(
        CorporateAction("BBB", "split", d[1], None, D(2), None), session_time(d[0], 18)
    )
    data = research(bars, [state(d[0]), state(d[0], "BBB")], d[:3], [split])
    report = assess(data)
    assert "ABNORMAL_RETURN" in codes(report, "AAA", d[1])
    assert "ABNORMAL_RETURN" not in codes(report, "BBB", d[1])
    assert "ACTION_EXPLAINED_RETURN" in codes(report, "BBB", d[1])
    # Exactly the 30% daily limit is a legal move, not an anomaly.
    limit = research(
        [bar(d[0], close="1000"), bar(d[1], close="1300")], [state(d[0])], d[:2]
    )
    assert "ABNORMAL_RETURN" not in codes(assess(limit))


def test_missing_bar_is_reported_never_filled_and_halt_is_distinguished():
    d = sessions(5)
    bars = [bar(d[0]), bar(d[1]), bar(d[3]), bar(d[4])]
    bars += [bar(d[0], "HLT"), bar(d[4], "HLT")]
    states = [state(d[0]), state(d[0], "HLT"), state(d[1], "HLT", "SUSPENDED"), state(d[3], "HLT")]
    data = research(bars, states, d)
    report = assess(data)
    assert ("MISSING_BAR", "AAA", d[2]) in {(i.code, i.symbol, i.session) for i in report.issues}
    assert "HALTED_NO_BAR" in codes(report, "HLT", d[2])
    assert "MISSING_BAR" in codes(report, "HLT", d[3])
    clean = quarantine(data, report)
    assert len(clean.snapshot.bars) == len(bars)  # nothing synthesized, nothing removed
    assert all(b.session != d[2] for b in clean.snapshot.bars if b.symbol == "AAA")


def test_unknown_state_and_zero_volume_are_reported():
    d = sessions(2)
    data = research(
        [bar(d[0]), bar(d[1], volume=0), bar(d[0], "NEW")], [state(d[0])], d
    )
    report = assess(data)
    assert "UNKNOWN_SECURITY_STATE" in codes(report, "NEW", d[0])
    assert "ZERO_VOLUME" in codes(report, "AAA", d[1])
    assert {i.severity for i in report.issues if i.code == "ZERO_VOLUME"} == {"info"}


def test_quarantine_removes_only_flagged_bars_and_keeps_the_clock():
    d = sessions(4)
    # A one-day spike: only the spike is held back; the next normal price is kept.
    bars = [bar(d[0], close="1000"), bar(d[1], close="2000"), bar(d[2], close="1000")]
    data = research(bars, [state(d[0])], d[:3])
    report = assess(data)
    clean = quarantine(data, report)
    assert [b.session for b in clean.snapshot.bars] == [d[0], d[2]]
    assert clean.sessions == data.sessions
    assert report.summary()["quarantined_bars"] == 1
    assert set(QUARANTINE_CODES) >= {"ABNORMAL_RETURN"}


def test_unexplained_level_shift_stays_quarantined_instead_of_fake_momentum():
    d = sessions(4)
    bars = [bar(day, close="1000" if i == 0 else "2000") for i, day in enumerate(d)]
    report = assess(research(bars, [state(d[0])], d))
    assert report.quarantined == {("AAA", day) for day in d[1:]}


def test_missing_after_last_bar_only_when_still_listed_normal():
    d = sessions(4)
    data = research(
        [bar(d[0]), bar(d[1]), bar(d[0], "GON"), bar(d[1], "GON")],
        [state(d[0]), state(d[0], "GON"), state(d[2], "GON", "DELISTED")],
        d,
    )
    report = assess(data)
    assert {(i.symbol, i.session) for i in report.issues if i.code == "MISSING_BAR"} == {
        ("AAA", d[2]),
        ("AAA", d[3]),
    }


def test_report_is_deterministic_and_serializable():
    d = sessions(3)
    data = research([bar(d[0]), bar(d[2], close="5000")], [state(d[0])], d)
    first, second = assess(data).to_dict(), assess(data).to_dict()
    assert first == second
    json.dumps(first)
    assert first["summary"]["by_code"]["MISSING_BAR"] == 1


def test_cli_screen_writes_accepted_and_quarantine_files_and_preserves_source(tmp_path):
    from momentum.agent.cli import parser, run

    d = sessions(3)
    source = tmp_path / "raw.csv"
    with source.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows([row(d[0]), row(d[1], open="0"), row(d[2])])
    before = source.read_bytes()
    out = tmp_path / "screened"
    result = run(parser().parse_args(["quality-screen", "--csv", str(source), "--output", str(out)]))
    assert source.read_bytes() == before
    assert result["accepted"] == 2 and result["quarantined"] == 1
    with (out / "prices.accepted.csv").open(encoding="utf-8") as handle:
        assert [r["session"] for r in csv.DictReader(handle)] == [d[0].isoformat(), d[2].isoformat()]
    with (out / "prices.quarantine.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["quality_codes"] == "NONPOSITIVE_PRICE" and rows[0]["line"] == "3"
    report = json.loads((out / "quality-screen.json").read_text(encoding="utf-8"))
    assert report["source_sha256"] and report["accepted_sha256"]


def test_screen_rejects_missing_columns():
    from momentum.agent.quality import ScreenError

    with pytest.raises(ScreenError):
        screen_price_rows([{"session": "2024-01-02"}])
