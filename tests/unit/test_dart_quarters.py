"""v30 quarterly operating income over assets, from synthetic OpenDART answers (no network)."""

import json
import statistics
from datetime import date, datetime, timedelta

import pytest

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError
from momentum.krx.dart_financials import annual_filings
from momentum.krx.dart_quarters import (
    build_quarters,
    interim_filings,
    quarter_ends,
    read_rows,
    volatility,
)


def filing(rcept_no, title, corp="00000001", stock="000010"):
    return {"rcept_no": rcept_no, "rcept_dt": rcept_no[:8], "corp_code": corp, "corp_cls": "Y",
            "stock_code": stock, "report_nm": title}


LIST = [
    filing("20230515000001", "분기보고서 (2023.03)"),
    filing("20230601000002", "[기재정정]분기보고서 (2023.03)"),
    filing("20230814000003", "반기보고서 (2023.06)"),
    filing("20231114000004", "분기보고서 (2023.09)"),
    filing("20240315000005", "사업보고서 (2023.12)"),
    filing("20231120000006", "분기보고서제출기한연장신고서 (2023.09)"),
]


def row(report, name, amount, cum=None, rcept="20230515000001", fs="CFS", end="2023.03.31", corp="00000001",
        stock="000010"):
    dt = f"{end} 현재" if name == "자산총계" else f"2023.01.01 ~ {end}"
    return {"corp_code": corp, "stock_code": stock, "bsns_year": "2023", "reprt_code": report, "rcept_no": rcept,
            "fs_div": fs, "account_nm": name, "thstrm_dt": dt, "thstrm_amount": amount, "thstrm_add_amount": cum}


def year_rows():
    return [
        row("11013", "영업이익", "10", "10", rcept="20230601000002"),                   # corrected: latest values
        row("11013", "자산총계", "1,000", rcept="20230601000002"),
        row("11013", "영업이익", "999", "999"),                                         # superseded batch
        row("11012", "영업이익", "20", "30", rcept="20230814000003", end="2023.06.30"),
        row("11012", "자산총계", "1,000", rcept="20230814000003", end="2023.06.30"),
        row("11012", "영업이익", "7", "7", rcept="20230814000003", fs="OFS", end="2023.06.30"),
        row("11014", "영업이익", "-5", "25", rcept="20231114000004", end="2023.09.30"),
        row("11014", "자산총계", "500", rcept="20231114000004", end="2023.09.30"),
        row("11011", "영업이익", "60", rcept="20240315000005", end="2023.12.31"),
        row("11011", "자산총계", "2,000", rcept="20240315000005", end="2023.12.31"),
    ]


def pages(tmp_path, rows=LIST):
    page = tmp_path / "page-1.json"
    page.write_text(json.dumps({"status": "000", "list": rows}, ensure_ascii=False), encoding="utf-8")
    return [page]


def test_interim_titles_give_originals_by_period(tmp_path):
    filings = interim_filings(pages(tmp_path))
    assert [(f.rcept_no[-1], f.period, f.original) for f in filings] == [
        ("1", "2023-03", True), ("2", "2023-03", False), ("3", "2023-06", True), ("4", "2023-09", True)]


def test_three_month_values_q4_from_the_annual_less_nine_months_and_original_receptions(tmp_path):
    filings = interim_filings(pages(tmp_path)) + annual_filings(pages(tmp_path))
    quarters, report = build_quarters(year_rows(), filings)
    got = [(q["symbol"], q["period_end"], q["operating_income"], q["assets"]) for q in quarters]
    assert got == [("000010", "2023-03-31", "10", "1000"), ("000010", "2023-06-30", "20", "1000"),
                   ("000010", "2023-09-30", "-5", "500"), ("000010", "2023-12-31", "35", "2000")]
    assert [q["ratio"] for q in quarters] == pytest.approx([0.01, 0.02, -0.01, 0.0175])
    when = [q["available_at"] for q in quarters]
    assert when == [KRX.disclosure_at(d).isoformat()
                    for d in (date(2023, 5, 15), date(2023, 8, 14), date(2023, 11, 14), date(2024, 3, 15))]
    assert report["quarters"] == 4 and report["fallback_api_reception"] == 0
    assert report["cumulative_mismatch"] == 0                                    # 10 + 20 == 30, 30 - 5 == 25


def test_missing_values_stay_missing_and_other_fiscal_years_are_left_out(tmp_path):
    rows = [r for r in year_rows() if not (r["reprt_code"] == "11014" and r["account_nm"] == "영업이익")]
    rows.append(row("11013", "영업이익", "3", "3", rcept="20230515000009", end="2023.06.30", corp="00000002",
                    stock="000020"))
    rows.append(row("11013", "자산총계", "300", rcept="20230515000009", end="2023.06.30", corp="00000002",
                    stock="000020"))
    quarters, report = build_quarters(rows, interim_filings(pages(tmp_path)))
    assert [q["period_end"] for q in quarters] == ["2023-03-31", "2023-06-30"]   # no Q3 income -> no Q3, no Q4
    assert report["other_fiscal_year"] == 1 and report["missing"] == {"operating_income": 2, "assets": 0}
    assert report["fallback_api_reception"] == 1                                 # annual: no original passed in


def test_a_symbol_with_two_values_for_one_quarter_is_undefined(tmp_path):
    rows = year_rows() + [row("11013", "영업이익", "1", "1", rcept="20230515000008", corp="00000003"),
                          row("11013", "자산총계", "100", rcept="20230515000008", corp="00000003")]
    with pytest.raises(SpecUndefinedError):
        build_quarters(rows, interim_filings(pages(tmp_path)))


def test_read_rows_takes_annual_and_interim_batches(tmp_path):
    for folder, header in (("dart_fin/2023", {"bsns_year": 2023, "corp_codes": ["00000001"]}),
                           ("dart_finq/2023-11013", {"bsns_year": 2023, "corp_codes": ["00000001"],
                                                     "reprt_code": "11013"})):
        (tmp_path / folder).mkdir(parents=True)
        body = {"status": "000", "list": [row("11011" if "fin/" in folder else "11013", "자산총계", "1")]}
        (tmp_path / folder / "a.json").write_bytes(json.dumps(header).encode() + b"\n" + json.dumps(body).encode())
        (tmp_path / folder / "b.json").write_bytes(json.dumps(header).encode() + b"\n" + json.dumps(
            {"status": "000", "list": [row("11013", n, "1", rcept="20230515000007") for n in ("자본총계", "부채총계")]}
        ).encode())
    got = read_rows(tmp_path)
    assert sorted((r["reprt_code"], r["account_nm"]) for r in got) == [
        ("11011", "자산총계"), ("11013", ""), ("11013", "자산총계")]               # other accounts: one marker
    assert {r["fs_div"] for r in got if not r["account_nm"]} == {"CFS"}


def q(end, ratio, available):
    return {"period_end": end.isoformat(), "ratio": ratio, "available_at": available.isoformat()}


def test_volatility_of_the_last_eight_quarters_known_at_the_decision():
    ends = quarter_ends(date(2023, 12, 31), 9)
    assert ends[0] == date(2021, 12, 31) and ends[-1] == date(2023, 12, 31) and ends[3] == date(2022, 9, 30)
    at = datetime(2024, 4, 1, 18, tzinfo=KRX.disclosure_at(date(2024, 1, 1)).tzinfo)
    ratios = [0.01, 0.03, 0.02, 0.05, 0.01, 0.02, 0.04, 0.0, 0.09]
    quarters = [q(e, r, KRX.disclosure_at(e + timedelta(days=45))) for e, r in zip(ends, ratios, strict=True)]
    assert volatility(quarters, at) == pytest.approx(statistics.stdev(ratios[1:]))    # the oldest is a ninth
    late = [*quarters[:-1], q(ends[-1], 0.09, at + timedelta(days=1))]              # Q4 not yet public
    assert volatility(late, at) == pytest.approx(statistics.stdev(ratios[:-1]))
    assert volatility(quarters[-5:], at) is None                                    # 5 of 8
    assert volatility(quarters[1:3] + quarters[4:], at) == pytest.approx(
        statistics.stdev(ratios[1:3] + ratios[4:]))                                 # 7 of 8, one gap
    assert volatility(quarters, at + timedelta(days=200)) is None                   # latest quarter is stale
