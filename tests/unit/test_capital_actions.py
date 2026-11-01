"""Bonus issues and capital reductions from detail answers shaped like 2024 ones."""

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from momentum.agent.contracts import AnnouncedAction
from momentum.config import SpecUndefinedError
from momentum.krx.capital_actions import build, korean_day, number
from momentum.krx.dart_collect import DartDetailFetcher
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.fsc_collect import CollectError
from momentum.ledger.ledger import CorporateAction

SESSIONS = tuple(
    date(2024, m, d)
    for m, d in [(4, 4), (4, 5), (4, 8), (4, 9), (4, 29), (5, 21), (5, 22), (5, 23)]
)
CONFIRMED = date(2024, 9, 30)
KEY = "dart-test-key-0123456789"


def row(rcept_no, code, name, report, day):
    return {
        "corp_code": "00" + code,
        "corp_name": name,
        "stock_code": code,
        "corp_cls": "K",
        "report_nm": report,
        "rcept_no": rcept_no,
        "flr_nm": name,
        "rcept_dt": day,
        "rm": "",
    }


ROWS = [
    row(
        "20240314000001",
        "408900",
        "스튜디오미르",
        "주요사항보고서(무상증자결정)",
        "20240314",
    ),
    row(
        "20240320000002",
        "100840",
        "SNT에너지",
        "주요사항보고서(무상증자결정)",
        "20240320",
    ),
    row(
        "20240314000003", "049470", "비트플래닛", "주요사항보고서(감자결정)", "20240314"
    ),
    row("20240320000004", "085370", "루트로닉", "주요사항보고서(감자결정)", "20240320"),
    row(
        "20240320000005",
        "119860",
        "커넥트웨이브",
        "주요사항보고서(감자결정)",
        "20240320",
    ),
    row(
        "20240320000006",
        "003550",
        "LG",
        "주요사항보고서(무상증자결정)(자회사의 주요경영사항)",
        "20240320",
    ),
]
DETAILS = {
    "20240314000001": {
        "nstk_ascnt_ps_ostk": "5",
        "nstk_ascnt_ps_iostk": "-",
        "nstk_asstd": "2024년 04월 08일",
        "nstk_lstprd": "2024년 04월 26일",
    },
    "20240320000002": {
        "nstk_ascnt_ps_ostk": "2",
        "nstk_asstd": "2024년 04월 08일",
        "nstk_lstprd": "2024년 05월 17일",
    },
    "20240314000003": {
        "cr_mth": "보통주 10주를 1주로 무상병합",
        "cr_rt_ostk": "90.0",
        "cr_std": "2024년 04월 29일",
        "crsc_nstklstprd": "2024년 05월 22일",
    },
    "20240320000004": {
        "cr_mth": "유상소각\n1주당 37,579.20원 상환",
        "cr_rt_ostk": "-",
        "cr_std": "2024년 04월 29일",
    },
    "20240320000005": {
        "cr_mth": "자기 주식 소각",
        "cr_rt_ostk": "3.09",
        "cr_std": "2024년 04월 29일",
    },
}
ADJUSTMENTS = [
    {"session": "2024-04-05", "symbol": "408900", "factor": "5.993322"},
    {"session": "2024-04-05", "symbol": "100840", "factor": "2.755187"},
    {
        "session": "2024-05-22",
        "symbol": "049470",
        "factor": "0.104500",
        "share_ratio": "0.099998",
    },
]


def events(tmp_path):
    page = tmp_path / "page-1.json"
    page.write_text(json.dumps({"status": "000", "list": ROWS}), encoding="utf-8")
    return parse_list_pages([page])


def test_korean_dates_and_numbers():
    assert korean_day("2024년 04월 17일") == date(2024, 4, 17)
    assert korean_day("-") is None and number("13,174,072") == D(13174072)
    with pytest.raises(SpecUndefinedError, match="unreadable date"):
        korean_day("2024/04/17")


def test_price_confirmed_bonus_and_reduction(tmp_path):
    result = build(events(tmp_path), DETAILS, ADJUSTMENTS, SESSIONS, CONFIRMED)
    bonus, reduction = result.actions
    assert (bonus["action_type"], bonus["ex_date"], bonus["pay_date"]) == (
        "bonus_issue",
        "2024-04-05",
        "2024-04-26",
    )
    assert bonus["split_ratio"] == "5" and bonus["available_at"].startswith(
        "2024-03-14T18:00"
    )
    assert reduction["action_type"] == "capital_reduction"
    assert (reduction["ex_date"], D(reduction["split_ratio"])) == (
        "2024-05-22",
        D("0.1"),
    )
    for action in result.actions:
        AnnouncedAction(
            CorporateAction(
                action["security_id"],
                action["action_type"],
                date.fromisoformat(action["ex_date"]),
                date.fromisoformat(action["pay_date"]) if action["pay_date"] else None,
                D(action["split_ratio"]),
                None,
            ),
            __import__("datetime").datetime.fromisoformat(action["available_at"]),
        )
    reasons = {u["stock_code"]: u["reason"] for u in result.unresolved}
    assert reasons["100840"].startswith("price adjustment 2.755187 on 2024-04-05")
    assert reasons["085370"] == "paid capital reduction is not supported"
    assert [s["stock_code"] for s in result.skipped] == ["119860"]
    assert "003550" not in reasons  # subsidiary filing ignored


def test_late_filing_missing_detail_and_existing_split(tmp_path):
    late = [dict(r) for r in ROWS[:1]]
    late[0]["rcept_dt"] = "20240405"
    late[0]["rcept_no"] = "20240405000001"
    page = tmp_path / "late.json"
    page.write_text(json.dumps({"status": "000", "list": late}), encoding="utf-8")
    details = {"20240405000001": DETAILS["20240314000001"]}
    result = build(parse_list_pages([page]), details, ADJUSTMENTS, SESSIONS, CONFIRMED)
    assert result.unresolved[0]["reason"] == "filed after the ex-date open"
    missing = build(events(tmp_path), {}, ADJUSTMENTS, SESSIONS, CONFIRMED)
    assert {u["reason"] for u in missing.unresolved} == {"detail answer not collected"}
    split = [{"security_id": "049470", "ex_date": "2024-05-22"}]
    result = build(events(tmp_path), DETAILS, ADJUSTMENTS, SESSIONS, CONFIRMED, split)
    assert [a["action_type"] for a in result.actions] == ["bonus_issue"]
    assert {s["reason"] for s in result.skipped} >= {
        "same day already has a split action"
    }


def test_detail_fetcher_and_tool_round_trip(tmp_path, capsys):
    root = tmp_path / "raw"
    period = root / "dart_list" / "B" / "2024-01-01_2024-03-31"
    period.mkdir(parents=True)
    (period / "page-1.json").write_text(
        json.dumps({"status": "000", "list": ROWS}), encoding="utf-8"
    )
    (period / "fetch.json").write_text(json.dumps({"pages": [{"file": "page-1.json"}]}))
    asked = []

    def dart(url):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert query["crtfc_key"] == [KEY] and query["bgn_de"] == query["end_de"]
        corp = query["corp_code"][0]
        rcept = next(r["rcept_no"] for r in ROWS if r["corp_code"] == corp)
        asked.append((parts.path.rsplit("/", 1)[1].removesuffix(".json"), rcept))
        if rcept == "20240320000005":
            return 200, b'{"status":"013","message":"no data"}'
        item = {"rcept_no": rcept, **DETAILS[rcept]}
        return 200, json.dumps({"status": "000", "list": [item]}).encode()

    adjustments = tmp_path / "adjustments.jsonl"
    adjustments.write_text("".join(json.dumps(a) + "\n" for a in ADJUSTMENTS))
    calendar = tmp_path / "calendar"
    calendar.mkdir()
    (calendar / "calendar.json").write_text(
        json.dumps([d.isoformat() for d in SESSIONS])
    )
    (calendar / "calendar-report.json").write_text(
        json.dumps({"confirmed_through": CONFIRMED.isoformat()})
    )
    spec = importlib.util.spec_from_file_location(
        "capital_actions_tool",
        Path(__file__).resolve().parents[2] / "tools/capital_actions.py",
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    out = tmp_path / "out"
    args = [
        "--dart-root",
        str(root),
        "--adjustments",
        str(adjustments),
        "--calendar",
        str(calendar),
        "--out",
        str(out),
    ]
    code = tool.main(
        args, environ={"OPENDART_API_KEY": KEY}, http=dart, sleep=lambda s: None
    )
    assert code == 1  # one detail answer was status 013
    assert capsys.readouterr().out.strip() == (
        "capital-actions: 5 filings, 1 bonus issues, 1 reductions, "
        "3 unresolved, 0 skipped, 1 problems"
    )
    assert ("fricDecsn", "20240314000001") in asked and (
        "crDecsn",
        "20240314000003",
    ) in asked
    assert len(json.loads((out / "actions.json").read_text())) == 2
    fetcher = DartDetailFetcher(
        root, "crDecsn", environ={"OPENDART_API_KEY": KEY}, http=dart
    )
    assert fetcher.fetch("20240314000003", "00049470", date(2024, 3, 14)) == "skipped"
    with pytest.raises(CollectError, match="unsupported detail API"):
        DartDetailFetcher(root, "piicDecsn", environ={"OPENDART_API_KEY": KEY})


def test_answer_for_an_original_carries_its_latest_correction(tmp_path):
    from momentum.krx.capital_actions import load_details

    # 프로브잇 2024: the 2023-12-27 filing (rm "정") came back as its
    # 2024-01-12 correction, under the correction's own number.
    original = dict(ROWS[0], rm="정")
    correction = dict(
        ROWS[0],
        rcept_no="20240320000009",
        rcept_dt="20240320",
        report_nm="[기재정정]주요사항보고서(무상증자결정)",
    )
    page = tmp_path / "page.json"
    page.write_text(
        json.dumps({"status": "000", "list": [original, correction]}), encoding="utf-8"
    )
    saved = tmp_path / "raw" / "dart_detail" / "fricDecsn"
    saved.mkdir(parents=True)
    item = {"rcept_no": "20240320000009", **DETAILS["20240314000001"]}
    (saved / "20240314000001.json").write_text(
        json.dumps({"status": "000", "list": [item]}), encoding="utf-8"
    )
    details = load_details(tmp_path / "raw")
    assert set(details) == {"20240320000009"}
    result = build(parse_list_pages([page]), details, ADJUSTMENTS, SESSIONS, CONFIRMED)
    (action,) = result.actions
    assert action["evidence"]["rcept_no"] == "20240320000009"
    assert action["available_at"].startswith("2024-03-20T18:00")
    assert result.skipped == [
        {
            "rcept_no": "20240314000001",
            "stock_code": "408900",
            "reason": "values only in another version",
        }
    ]
    (saved / "other.json").write_text(
        json.dumps({"status": "000", "list": [dict(item, nstk_ascnt_ps_ostk="4")]})
    )
    with pytest.raises(SpecUndefinedError, match="conflicting item"):
        load_details(tmp_path / "raw")


def test_unconfirmed_reduction_reports_what_the_price_did(tmp_path):
    moved = [dict(a) for a in ADJUSTMENTS]
    moved[2]["share_ratio"] = "0.194000"  # shares fell by a different fraction
    result = build(events(tmp_path), DETAILS, moved, SESSIONS, CONFIRMED)
    (entry,) = [u for u in result.unresolved if u["stock_code"] == "049470"]
    assert entry["reason"] == "price adjustment does not confirm the reduction"
    assert entry["observed"] == {"2024-05-22": "price 0.104500, shares 0.194000"}
    assert D(entry["expected_factor"]) == D("0.1")
    treasury = [s for s in result.skipped if s["stock_code"] == "119860"]
    assert (
        treasury[0]["reason"] == "treasury share cancellation does not change holdings"
    )


def test_reduction_relisted_during_a_halt_is_confirmed(tmp_path):
    halted = [dict(a) for a in ADJUSTMENTS[:2]] + [
        {
            "session": "2024-05-21",
            "symbol": "049470",
            "factor": "0.100000",
            "share_ratio": "0.100000",
            "kind": "HALTED_SHARE_CHANGE",
        }
    ]
    result = build(events(tmp_path), DETAILS, halted, SESSIONS, CONFIRMED)
    (reduction,) = [
        a for a in result.actions if a["action_type"] == "capital_reduction"
    ]
    assert reduction["ex_date"] == "2024-05-21"


def test_bonus_confirmed_by_whole_company_growth_when_treasury_excluded(tmp_path):
    # 000220-like 2020s: 1 share per share, treasury shares excluded, so the
    # base moved 1.926606 = 1 + 9,000,000 / 9,673,000 (not 2).
    details = dict(DETAILS)
    details["20240314000001"] = dict(
        DETAILS["20240314000001"],
        nstk_ascnt_ps_ostk="1",
        nstk_ostk_cnt="9,000,000",
        bfic_tisstk_ostk="9,673,000",
    )
    moved = [dict(a) for a in ADJUSTMENTS]
    moved[0]["factor"] = "1.929000"
    result = build(events(tmp_path), details, moved, SESSIONS, CONFIRMED)
    bonus = [a for a in result.actions if a["action_type"] == "bonus_issue"]
    assert bonus[0]["split_ratio"] == "1"
    assert bonus[0]["evidence"]["confirmed_by"] == "1 + new shares / shares before"
    # Without the share counts the same move stays unconfirmed.
    del details["20240314000001"]["nstk_ostk_cnt"]
    again = build(events(tmp_path), details, moved, SESSIONS, CONFIRMED)
    assert not [a for a in again.actions if a["action_type"] == "bonus_issue"]


def test_reduction_confirmed_when_shares_and_price_sit_within_30pct_of_the_filing(tmp_path):
    # 024810 2021-05-24: filing 10:1 (0.1), base moved 0.113964, shares 0.103480 (old 0.5% rule left it out)
    loose = [dict(a) for a in ADJUSTMENTS]
    loose[2] = {"session": "2024-05-22", "symbol": "049470", "factor": "0.113964", "share_ratio": "0.103480"}
    result = build(events(tmp_path), DETAILS, loose, SESSIONS, CONFIRMED)
    (reduction,) = [a for a in result.actions if a["action_type"] == "capital_reduction"]
    assert D(reduction["split_ratio"]) == D("0.1")
    assert reduction["ex_date"] == "2024-05-22" and "30%" in reduction["evidence"]["confirmed_by"]
    # shares 20% off but the price five times off the filing: not this reduction
    far = [dict(a) for a in ADJUSTMENTS]
    far[2] = {"session": "2024-05-22", "symbol": "049470", "factor": "0.500000", "share_ratio": "0.120000"}
    result = build(events(tmp_path), DETAILS, far, SESSIONS, CONFIRMED)
    assert not [a for a in result.actions if a["action_type"] == "capital_reduction"]


def test_a_strict_match_wins_over_an_earlier_banded_day(tmp_path):
    # 344860 2020: the 30% band matched three sessions before the exact relisting; the exact day is the relisting
    two = [dict(a) for a in ADJUSTMENTS]
    two[2] = {"session": "2024-05-21", "symbol": "049470", "factor": "0.110000", "share_ratio": "0.120000"}
    two.append({"session": "2024-05-22", "symbol": "049470", "factor": "0.104500", "share_ratio": "0.099998"})
    result = build(events(tmp_path), DETAILS, two, SESSIONS, CONFIRMED)
    (reduction,) = [a for a in result.actions if a["action_type"] == "capital_reduction"]
    assert reduction["ex_date"] == "2024-05-22" and reduction["evidence"]["confirmed_by"] == "listed shares within 0.5%"


def test_two_reductions_confirming_one_relisting_day_are_not_both_booked():
    from momentum.krx.capital_actions import _one_per_day

    def red(rcept, ratio, by, avail="2024-03-14T18:00:00+09:00"):
        return {"security_id": "049470", "action_type": "capital_reduction", "ex_date": "2024-05-22",
                "split_ratio": ratio, "available_at": avail, "evidence": {"rcept_no": rcept, "confirmed_by": by}}

    strict = "listed shares within 0.5%"
    band = "listed shares and base price within 30% (user decision 2026-11-01)"
    kept, dropped = _one_per_day([red("A", "0.1", strict), red("B", "0.12", band)])
    assert [a["evidence"]["rcept_no"] for a in kept] == ["A"] and dropped[0]["rcept_no"] == "B"
    kept, dropped = _one_per_day([red("A", "0.1", band), red("B", "0.12", band)])
    assert kept == [] and {d["rcept_no"] for d in dropped} == {"A", "B"}
    assert dropped[0]["reason"] == "several filings confirm the same relisting day"
    kept, _ = _one_per_day([red("A", "0.1", band, "2024-03-20T18:00:00+09:00"), red("B", "0.1", band)])
    assert [a["evidence"]["rcept_no"] for a in kept] == ["B"]          # same fraction twice: the earlier filing
    bonus = {**red("C", "1", band), "action_type": "bonus_issue"}
    assert _one_per_day([bonus, red("A", "0.1", strict)])[0] == [bonus, red("A", "0.1", strict)]


def test_a_deferred_base_ratio_split_is_booked_unless_a_reduction_takes_its_day(tmp_path):
    def cand(day, ratio):
        return {"security_id": "049470", "action_type": "split", "ex_date": day, "pay_date": None,
                "split_ratio": ratio, "cash_amount": None, "available_at": "2024-03-01T18:00:00+09:00",
                "evidence": {"rcept_no": "S1"}}

    result = build(events(tmp_path), DETAILS, ADJUSTMENTS, SESSIONS, CONFIRMED,
                   candidates=[cand("2024-05-22", "0.125"), cand("2024-04-29", "0.1")])
    days = {(a["action_type"], a["ex_date"]) for a in result.actions if a["security_id"] == "049470"}
    assert ("capital_reduction", "2024-05-22") in days and ("split", "2024-05-22") not in days
    assert ("split", "2024-04-29") in days                                   # no reduction that day: booked
    assert any(s["reason"] == "base-ratio split replaced by the filed reduction" for s in result.skipped)
