"""RES-01b: one preregistered ETF's prices and states from saved FSC ETF pages."""

import json
from datetime import date

import pytest

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.krx.benchmark_etf import build_benchmark_etf
from momentum.krx.fsc_states import status_events

DAYS = (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5))


def etf_row(code, isin, close, *, index="코스피 200", halted=False):
    return {
        "basDt": "",
        "srtnCd": code,
        "isinCd": isin,
        "itmsNm": "FIXTURE ETF",
        "mkp": "0" if halted else str(close),
        "hipr": "0" if halted else str(close + 50),
        "lopr": "0" if halted else str(close - 50),
        "clpr": str(close),
        "trqu": "0" if halted else "1000",
        "bssIdxIdxNm": index,
    }


def write_etf_day(root, day, rows):
    folder = root / "etf" / day
    folder.mkdir(parents=True)
    for row in rows:
        row["basDt"] = day.replace("-", "")
    body = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {"pageNo": 1, "totalCount": len(rows), "items": {"item": rows}},
        }
    }
    (folder / "page-1.json").write_text(json.dumps(body, ensure_ascii=False))
    (folder / "fetch.json").write_text(
        json.dumps(
            {
                "requested_date": day,
                "total_count": len(rows),
                "pages": [{"file": "page-1.json", "rows": len(rows)}],
            }
        )
    )


A, B = ("069500", "KR7069500007"), ("123456", "KR7123456001")


def fixture(root, *, other_index="코스닥 150"):
    write_etf_day(root, "2024-06-03", [etf_row(*A, 35000), etf_row(*B, 9000, index=other_index)])
    write_etf_day(root, "2024-06-04", [etf_row(*A, 35100, halted=True), etf_row(*B, 9010, index=other_index)])
    write_etf_day(root, "2024-06-05", [etf_row(*A, 35200), etf_row(*B, 9020, index=other_index)])


def test_builds_only_the_named_etf_with_declared_market(tmp_path):
    fixture(tmp_path)
    rows, records, report = build_benchmark_etf(tmp_path, DAYS, "069500", "코스피 200")
    assert {r["symbol"] for r in rows} == {"069500"}
    # The halted day is no-trade evidence, not a bar.
    assert [r["session"] for r in rows] == ["2024-06-03", "2024-06-05"]
    assert [(r["status"], r["asset_type"], r["market"]) for r in records] == [
        ("NORMAL", "ETF", "KOSPI"),
        ("SUSPENDED", "ETF", "KOSPI"),
        ("NORMAL", "ETF", "KOSPI"),
    ]
    assert records[0]["effective_at"] == KRX.open_at(DAYS[0]).isoformat()
    assert not any(r["leveraged"] or r["inverse"] for r in records)
    assert report["no_trade"] == 1 and report["quotes"] == 3
    assert "distributions" in report["coverage"]


def test_unexpected_index_or_missing_session_is_refused(tmp_path):
    fixture(tmp_path)
    with pytest.raises(SpecUndefinedError, match="index names"):
        build_benchmark_etf(tmp_path, DAYS, "069500", "코스피 200 레버리지")
    with pytest.raises(SpecUndefinedError, match="no ETF quote"):
        build_benchmark_etf(tmp_path, DAYS, "999999", "코스피 200")
    with pytest.raises(SpecUndefinedError, match="not collected"):
        build_benchmark_etf(tmp_path, (*DAYS, date(2024, 6, 7)), "069500", "코스피 200")


def test_status_events_market_override_and_symbol_filter(tmp_path):
    fixture(tmp_path)
    with pytest.raises(SpecUndefinedError, match="unknown market"):
        status_events(tmp_path, DAYS, "etf")  # ETF quotes carry no mrktCtg
    events = status_events(
        tmp_path, DAYS, "etf", market="KOSPI", symbols=frozenset({"123456"})
    )
    assert [(e.symbol, e.status, e.market) for e in events] == [
        ("123456", "NORMAL", "KOSPI")
    ]
    with pytest.raises(SpecUndefinedError, match="unknown market"):
        status_events(tmp_path, DAYS, "etf", market="NYSE")


def test_tool_writes_appendable_outputs(tmp_path):
    import csv
    import sys

    sys.path.insert(0, "tools")
    from benchmark_etf import main

    fixture(tmp_path / "fsc")
    (tmp_path / "cal").mkdir()
    (tmp_path / "cal" / "calendar.json").write_text(json.dumps([d.isoformat() for d in DAYS]))
    args = ["--fsc-root", str(tmp_path / "fsc"), "--calendar", str(tmp_path / "cal")]
    args += ["--symbol", "069500", "--expect-index", "코스피 200", "--out", str(tmp_path / "out")]
    assert main(args) == 0
    with (tmp_path / "out" / "prices.csv").open(encoding="utf-8") as handle:
        assert [r["close"] for r in csv.DictReader(handle)] == ["35000", "35200"]
    lines = (tmp_path / "out" / "securities.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["asset_type"] == "ETF"
    assert main([*args[:-2], "--out", str(tmp_path / "x"), "--expect-index", "다른 지수"]) == 1
