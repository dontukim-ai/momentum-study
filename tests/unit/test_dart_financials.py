"""Synthetic OpenDART annual financials (explore v7 §2): no real filings or network."""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
from trading_agent.fundamentals import read_fundamentals

from momentum.agent.market import KRX
from momentum.krx.dart_financials import (
    DartFinancialsFetcher,
    annual_filings,
    build_records,
    listed_corps,
    originals,
    read_batches,
)
from momentum.krx.fsc_collect import CollectError


def filing(rcept_no, corp, title, stock="000001", cls="Y"):
    return {"rcept_no": rcept_no, "rcept_dt": rcept_no[:8], "corp_code": corp, "corp_cls": cls,
            "stock_code": stock, "corp_name": "가상", "report_nm": title, "flr_nm": "가상", "rm": ""}


LIST = [
    filing("20240315000001", "00000001", "사업보고서 (2023.12)"),
    filing("20240320000002", "00000001", "[기재정정]사업보고서 (2023.12)"),
    filing("20240318000003", "00000002", "사업보고서 (2023.12)", stock="000002", cls="K"),
    filing("20240325000004", "00000003", "사업보고서제출기한연장신고서 (2023.12)", stock="000003"),
    filing("20240328000005", "00000004", "사업보고서 (2023.12)", stock="", cls="E"),
    filing("20240410000006", "00000002", "[첨부추가]사업보고서 (2023.12)", stock="000002", cls="K"),
    filing("20240514000007", "00000001", "분기보고서 (2024.03)"),
]


def write_list(root, rows=LIST):
    folder = root / "dart_list" / "A" / "2024-03-01_2024-05-31"
    folder.mkdir(parents=True)
    (folder / "page-1.json").write_text(json.dumps({"status": "000", "list": rows}, ensure_ascii=False))
    (folder / "fetch.json").write_text(json.dumps({"pages": [{"file": "page-1.json"}]}))


def account(corp, rcept, fs, name, amount, stock="000001", end="2023.12.31"):
    dt = f"{end} 현재" if name != "영업이익" else f"2023.01.01 ~ {end}"
    return {"corp_code": corp, "stock_code": stock, "bsns_year": "2023", "reprt_code": "11011",
            "rcept_no": rcept, "fs_div": fs, "account_nm": name, "thstrm_dt": dt, "thstrm_amount": amount}


def answer(*rows):
    return json.dumps({"status": "000", "list": list(rows)}, ensure_ascii=False).encode()


def test_annual_filings_originals_and_listed_companies(tmp_path):
    write_list(tmp_path)
    from momentum.krx.dart_collect import list_pages
    filings = annual_filings(list_pages(tmp_path, "A"))
    assert [(f.rcept_no[-1], f.original) for f in filings] == [
        ("1", True), ("2", False), ("3", True), ("5", True), ("6", False)]
    first = originals(filings)
    assert first[("00000001", "2023-12")].rcept_no == "20240315000001"
    assert ("00000003", "2023-12") not in first  # deadline-extension notice is not a report
    assert listed_corps(filings, 2023) == ["00000001", "00000002"]  # corp_cls E excluded
    assert listed_corps(filings, 2024) == []


def test_records_use_original_reception_latest_values_and_never_fill(tmp_path):
    write_list(tmp_path)
    from momentum.krx.dart_collect import list_pages
    filings = annual_filings(list_pages(tmp_path, "A"))
    rows = [
        # company 1: corrected later; API answers with the correction's number
        account("00000001", "20240320000002", "CFS", "자본총계", "1,000"),
        account("00000001", "20240320000002", "CFS", "자산총계", "5,000"),
        account("00000001", "20240320000002", "CFS", "영업이익", "-30"),
        account("00000001", "20240320000002", "OFS", "자본총계", "999"),
        # an older batch of the same company: superseded
        account("00000001", "20240315000001", "CFS", "자본총계", "900"),
        # company 2: separate statements only, no operating income
        account("00000002", "20240410000006", "OFS", "자본총계", "70", stock="000002"),
        account("00000002", "20240410000006", "OFS", "자산총계", "700", stock="000002"),
        # company 9: no annual filing in the saved list -> API reception date
        account("00000009", "20240620000009", "CFS", "자본총계", "1", stock="000009"),
        account("00000009", "20240620000009", "CFS", "자산총계", "2", stock="000009"),
    ]
    records, report = build_records(rows, filings)
    by = {r["symbol"]: r for r in records}
    one = by["000001"]
    assert (one["statement"], one["equity"], one["assets"], one["operating_income"]) == ("CFS", "1000", "5000", "-30")
    assert one["available_at"] == KRX.disclosure_at(date(2024, 3, 15)).isoformat()
    assert one["source"] == "dart-fnlttMultiAcnt:20240320000002;original:20240315000001"
    two = by["000002"]
    assert (two["statement"], two["operating_income"]) == ("OFS", None)
    assert two["available_at"] == KRX.disclosure_at(date(2024, 3, 18)).isoformat()
    assert by["000009"]["available_at"] == KRX.disclosure_at(date(2024, 6, 20)).isoformat()
    assert report["fallback_api_reception"] == 1 and report["fallbacks"] == [["00000009", "2023-12-31", "20240620000009"]]
    assert report["missing"] == {"equity": 0, "assets": 0, "operating_income": 2}
    assert report["statement"] == {"CFS": 2, "OFS": 1}


def test_fetcher_batches_resume_and_refuse_key_echo(tmp_path):
    calls = []

    def http(url):
        calls.append(url)
        return 200, answer(account("00000001", "20240315000001", "CFS", "자본총계", "1"))

    fetcher = DartFinancialsFetcher(tmp_path, environ={"OPENDART_API_KEY": "SECRET"}, http=http, sleep=lambda s: None)
    corps = [f"{i:08d}" for i in range(150)]
    assert fetcher.fetch_year(2023, corps) == {"fetched": 2, "skipped": 0}
    assert "corp_code=00000000%2C00000001" in calls[0] and "bsns_year=2023" in calls[0] and "reprt_code=11011" in calls[0]
    assert fetcher.fetch_year(2023, corps) == {"fetched": 0, "skipped": 2}
    assert len(calls) == 2
    assert [r["_requested_year"] for r in read_batches(tmp_path)] == [2023, 2023]
    echo = DartFinancialsFetcher(tmp_path / "x", environ={"OPENDART_API_KEY": "SECRET"},
                                 http=lambda url: (200, b'{"status":"000","list":[],"k":"SECRET"}'), sleep=lambda s: None)
    with pytest.raises(CollectError, match="echoes"):
        echo.fetch_year(2023, ["00000001"])
    bad = DartFinancialsFetcher(tmp_path / "y", environ={"OPENDART_API_KEY": "SECRET"},
                                http=lambda url: (200, b'{"status":"020"}'), sleep=lambda s: None)
    with pytest.raises(CollectError, match="020"):
        bad.fetch_year(2023, ["00000001"])
    assert not list((tmp_path / "x").rglob("*.json")) and not list((tmp_path / "y").rglob("*.json"))
    with pytest.raises(CollectError, match="not set"):
        DartFinancialsFetcher(tmp_path, environ={})


def test_cli_build_writes_records_kis_agent_reads(tmp_path, capsys):
    write_list(tmp_path)
    fetcher = DartFinancialsFetcher(
        tmp_path, environ={"OPENDART_API_KEY": "K"}, sleep=lambda s: None,
        http=lambda url: (200, answer(account("00000001", "20240320000002", "CFS", "자본총계", "1,000"),
                                      account("00000001", "20240320000002", "CFS", "자산총계", "5,000"),
                                      account("00000001", "20240320000002", "CFS", "영업이익", "10"))))
    fetcher.fetch_year(2023, ["00000001"])
    spec = importlib.util.spec_from_file_location("dart_financials_tool", Path(__file__).parents[2] / "tools/dart_financials.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.main(["build", "--root", str(tmp_path), "--out", str(tmp_path / "out")]) == 0
    assert "1 records" in capsys.readouterr().out
    (tmp_path / "shares.csv").write_text("symbol,session,through,available_at,listed_shares,source\n", encoding="utf-8")
    history = read_fundamentals(tmp_path / "out" / "financials.jsonl", tmp_path / "shares.csv")
    record = history.financials("000001", KRX.observed_at(date(2024, 3, 15)))
    assert (record.equity, record.assets, record.operating_income) == (1000, 5000, 10)
    assert history.financials("000001", KRX.observed_at(date(2024, 3, 14))) is None
    assert tool.main(["build", "--root", str(tmp_path / "empty"), "--out", str(tmp_path / "o2")]) == 1
