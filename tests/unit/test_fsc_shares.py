"""Listed-share runs from synthetic FSC quote days (explore v7 §3)."""

import csv
import importlib.util
from datetime import date
from pathlib import Path

import pytest
from test_fsc_adjustments import quote, write_day
from trading_agent.fundamentals import read_fundamentals

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.krx.fsc_shares import share_runs, write_runs

DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


def market(root):
    shares = {
        "A": [100, 100, 100, 150, 150],   # issue on day 4 starts a new run
        "B": [200, None, 200, 200, 200],  # absent on day 2: no carry-over
        "C": [300, "", 300, 300, 300],    # quoted without a count on day 2
    }
    for i, day in enumerate(DAYS):
        rows = []
        for code, counts in shares.items():
            count = counts[i]
            if count is not None:
                rows.append(quote(f"KR{code}", 1000, 0, count, code=code))
        write_day(root, day, rows)
    return [date.fromisoformat(d) for d in DAYS]


def test_runs_break_on_changes_gaps_and_missing_counts(tmp_path):
    sessions = market(tmp_path)
    runs, report = share_runs(tmp_path, sessions)
    got = [(r["symbol"], r["session"], r["through"], r["listed_shares"]) for r in runs]
    assert got == [
        ("A", "2024-01-02", "2024-01-04", "100"), ("A", "2024-01-05", "2024-01-08", "150"),
        ("B", "2024-01-02", "2024-01-02", "200"), ("B", "2024-01-04", "2024-01-08", "200"),
        ("C", "2024-01-02", "2024-01-02", "300"), ("C", "2024-01-04", "2024-01-08", "300"),
    ]
    assert runs[1]["available_at"] == KRX.historical_bar_at(date(2024, 1, 5)).isoformat()
    assert report == {"sessions": 5, "rows": 14, "missing_count_rows": 1, "runs": 6}


def test_written_runs_load_into_kis_agent_and_answer_per_session(tmp_path):
    sessions = market(tmp_path / "fsc")
    runs, _ = share_runs(tmp_path / "fsc", sessions)
    write_runs(runs, tmp_path / "out" / "shares.csv")
    (tmp_path / "financials.jsonl").write_text("", encoding="utf-8")
    history = read_fundamentals(tmp_path / "financials.jsonl", tmp_path / "out" / "shares.csv")
    late = KRX.observed_at(sessions[-1])
    assert [history.listed_shares("C", d, late) for d in sessions] == [300, None, 300, 300, 300]
    assert [history.listed_shares("A", d, late) for d in sessions] == [100, 100, 100, 150, 150]
    with (tmp_path / "out" / "shares.csv").open() as handle:
        assert next(csv.reader(handle)) == ["symbol", "session", "through", "available_at", "listed_shares", "source"]


def test_missing_session_and_repeated_symbol_stop(tmp_path):
    sessions = market(tmp_path)
    with pytest.raises(SpecUndefinedError, match="no saved FSC"):
        share_runs(tmp_path, [*sessions, date(2024, 1, 9)])
    write_day(tmp_path, "2024-01-09", [quote("KRA", 1, 0, 1, code="A"), quote("KRA", 1, 0, 1, code="A")])
    with pytest.raises(SpecUndefinedError, match="repeated"):
        share_runs(tmp_path, [*sessions, date(2024, 1, 9)])


def test_cli(tmp_path, capsys):
    sessions = market(tmp_path / "fsc")
    (tmp_path / "sessions.json").write_text('["' + '","'.join(d.isoformat() for d in sessions) + '"]')
    spec = importlib.util.spec_from_file_location("fsc_shares_tool", Path(__file__).parents[2] / "tools/fsc_shares.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.main(["--root", str(tmp_path / "fsc"), "--sessions", str(tmp_path / "sessions.json"),
                      "--out", str(tmp_path / "shares.csv")]) == 0
    assert "6 runs" in capsys.readouterr().out
