"""069500 distributions from a hand-made file (v31 benchmark, user decision 2026-11-04)."""

import json
from datetime import date

import pytest

from momentum.config import SpecUndefinedError
from momentum.krx.etf_distributions import build, read_rows, with_distributions

# 2024-04: record Tue 04-30, sessions ... 04-26 Fri, 04-29 Mon, 04-30 Tue, 05-02 Thu (05-01 off)
SESSIONS = (date(2024, 4, 26), date(2024, 4, 29), date(2024, 4, 30), date(2024, 5, 2), date(2024, 5, 3))
CONFIRMED = date(2024, 5, 31)
HEADER = "record_date,pay_date,cash_per_unit,announced\n"


def rows(tmp_path, *lines):
    path = tmp_path / "dist.csv"
    path.write_text(HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
    return read_rows(path)


def test_distribution_becomes_a_cash_dividend_with_ex_date_and_rolled_payment(tmp_path):
    [action] = build(rows(tmp_path, "2024-04-30,2024-05-01,250,2024-04-25"), "069500", SESSIONS, CONFIRMED)
    assert action == {
        "security_id": "069500", "action_type": "cash_dividend", "ex_date": "2024-04-29",
        "pay_date": "2024-05-02", "split_ratio": None, "cash_amount": "250",
        "available_at": "2024-04-25T18:00:00+09:00",
        "evidence": {"source": "manual distribution file", "record_date": "2024-04-30",
                     "pay_date_unrolled": "2024-05-01"},
    }


def test_an_amount_announced_on_or_after_the_ex_date_is_refused(tmp_path):
    with pytest.raises(SpecUndefinedError, match="announced 2024-04-29"):
        build(rows(tmp_path, "2024-04-30,2024-05-02,250,2024-04-29"), "069500", SESSIONS, CONFIRMED)


@pytest.mark.parametrize("line", ["2024-04-30,2024-05-02,0,2024-04-25", "2024-04-30,2024-05-02,1,250,x",
                                  "2024-04-30,2024-04-29,250,2024-04-25", "2024/04/30,2024-05-02,250,2024-04-25",
                                  "2024-04-30,2024-05-02,\"1,250\",2024-04-25"])
def test_unreadable_or_impossible_rows_are_refused(tmp_path, line):
    with pytest.raises(SpecUndefinedError):
        build(rows(tmp_path, line), "069500", SESSIONS, CONFIRMED)


def test_duplicate_record_dates_and_wrong_header_are_refused(tmp_path):
    with pytest.raises(SpecUndefinedError, match="duplicate"):
        build(rows(tmp_path, "2024-04-30,2024-05-02,250,2024-04-25", "2024-04-30,2024-05-02,260,2024-04-25"),
              "069500", SESSIONS, CONFIRMED)
    bad = tmp_path / "bad.csv"
    bad.write_text("date,amount\n2024-04-30,250\n", encoding="utf-8")
    with pytest.raises(SpecUndefinedError, match="header"):
        read_rows(bad)


def test_merging_into_a_research_input_copies_the_rest_and_refuses_existing_rows(tmp_path):
    research = tmp_path / "research"
    research.mkdir()
    for name in ("prices.csv", "securities.jsonl", "sessions.json"):
        (research / name).write_text(name, encoding="utf-8")
    base = [{"security_id": "005930", "action_type": "cash_dividend", "ex_date": "2024-04-29",
             "pay_date": "2024-05-20", "split_ratio": None, "cash_amount": "361", "available_at": "x"}]
    (research / "actions.json").write_text(json.dumps(base), encoding="utf-8")
    actions = build(rows(tmp_path, "2024-04-30,2024-05-01,250,2024-04-25"), "069500", SESSIONS, CONFIRMED)
    out = tmp_path / "research-dist"
    added = with_distributions(research, actions, out)
    assert added == 1
    merged = json.loads((out / "actions.json").read_text(encoding="utf-8"))
    assert [(a["security_id"], a["ex_date"]) for a in merged] == [("005930", "2024-04-29"), ("069500", "2024-04-29")]
    assert "evidence" not in merged[1]
    assert (out / "prices.csv").read_text(encoding="utf-8") == "prices.csv"
    with pytest.raises(SpecUndefinedError, match="exists"):
        with_distributions(research, actions, out)                     # never overwrite
    with pytest.raises(SpecUndefinedError, match="already has"):
        with_distributions(out, actions, tmp_path / "again")           # 069500 rows already in the input


def test_tool_writes_a_new_research_input_and_reports_years(tmp_path, capsys):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("etf_dist_tool", Path(__file__).resolve().parents[2]
                                                  / "tools/etf_distributions.py")
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    calendar = tmp_path / "calendar"
    calendar.mkdir()
    (calendar / "calendar.json").write_text(json.dumps([d.isoformat() for d in SESSIONS]))
    (calendar / "calendar-report.json").write_text(json.dumps({"confirmed_through": CONFIRMED.isoformat()}))
    research = tmp_path / "research"
    research.mkdir()
    for name in ("prices.csv", "securities.jsonl", "sessions.json"):
        (research / name).write_text("", encoding="utf-8")
    (research / "actions.json").write_text("[]", encoding="utf-8")
    dist = tmp_path / "dist.csv"
    dist.write_text(HEADER + "2024-04-30,2024-05-01,250,2024-04-25\n", encoding="utf-8")
    args = ["--file", str(dist), "--calendar", str(calendar), "--research", str(research)]
    assert tool.main([*args, "--out", str(tmp_path / "out")]) == 0
    printed = capsys.readouterr().out
    assert "1 added" in printed and "per year {'2024': 1}" in printed and "ex 2024-04-29 pay 2024-05-02" in printed
    assert tool.main([*args, "--out", str(tmp_path / "out")]) == 1          # never overwrite
