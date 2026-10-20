"""Dividend decisions from synthetic pages shaped like real 2024 documents."""

import io
import zipfile
from datetime import date
from decimal import Decimal as D

import pytest

from momentum.config import SpecUndefinedError
from momentum.krx.dart_dividends import (
    ex_dividend_date,
    load_documents,
    parse_dividend_document,
    read_document,
)


def cell(text, value=False, **attrs):
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    span = '<span class="xforms_input" style="width:1px">' if value else "<span>"
    return f"<td{extra}> {span}{text}</span> </td>"


def main_table(
    *,
    common="105",
    record="2023-12-31",
    pay="-",
    item8=("8. 주주총회 개최여부", "개최"),
):
    rows = [
        cell("1. 배당구분", colspan=2) + cell("결산배당", True, colspan=2),
        cell("2. 배당종류", colspan=2) + cell("현금배당", True, colspan=2),
        cell("- 현물자산의 상세내역", colspan=2) + cell("-", True, colspan=2),
        cell("3. 1주당 배당금(원)", rowspan=2)
        + cell("보통주식")
        + cell(common, True, colspan=2),
        cell("종류주식") + cell("-", True, colspan=2),
        cell("- 차등배당 여부", colspan=2) + cell("미해당", True, colspan=2),
        cell("4. 시가배당율(%)", rowspan=2)
        + cell("보통주식")
        + cell("2.9", True, colspan=2),
        cell("종류주식") + cell("-", True, colspan=2),
        cell("5. 배당금총액(원)", colspan=2) + cell("7,438,111,590", True, colspan=2),
        cell("6. 배당기준일", colspan=2) + cell(record, True, colspan=2),
        cell("7. 배당금지급 예정일자", colspan=2) + cell(pay, True, colspan=2),
        cell(item8[0], colspan=2) + cell(item8[1], True, colspan=2),
        cell("9. 주주총회 예정일자", colspan=2) + cell("2024-03-27", True, colspan=2),
        cell("10. 이사회결의일(결정일)", colspan=2)
        + cell("2024-02-07", True, colspan=2),
        cell("- 사외이사 참석여부", rowspan=2) + cell("참석(명)") + cell("3", True),
        cell("불참(명)") + cell("-", True),
        cell("11. 기타 투자판단과 관련한 중요사항", colspan=2)
        + cell(
            '변경될 수 있음<br xmlns:java="http://xml.apache.org/xalan/java">1개월 내 지급',
            True,
        ),
    ]
    body = "".join(f"<tr>{r}</tr>" for r in rows)
    return f'<table border="1" id="XFormD1_Form0_Table0"><tbody>{body}</tbody></table>'


def page(*tables):
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr">'
        '<title>회사/현금ㆍ현물 배당 결정</title></head><body><div class="xforms">'
        + "".join(tables)
        + "</div></body></html>"
    )


def test_ordinary_filing_fields():
    decision = parse_dividend_document("20240207801246", page(main_table()))
    assert decision.dividend_kind == "결산배당" and decision.payment_kind == "현금배당"
    assert decision.per_share_common == D(105) and decision.per_share_class is None
    assert decision.record_date == date(2023, 12, 31)
    assert decision.pay_date is None and decision.board_date == date(2024, 2, 7)
    assert decision.subsidiary is None and decision.correction is False
    assert decision.agm_date == date(2024, 3, 27)


def test_correction_form_version_and_comma_amounts():
    correction = (
        '<table id="XFormD8_Form0_Table1"><tbody><tr>'
        + cell("정정일자")
        + cell("2024-01-02", True)
        + "</tr></tbody></table>"
    )
    text = page(
        correction,
        main_table(common="1,050", pay="2024-04-30", item8=("8. 승인기관", "주주총회")),
    )
    decision = parse_dividend_document("20240102900164", text)
    assert decision.correction is True
    assert decision.per_share_common == D(1050) and decision.pay_date == date(
        2024, 4, 30
    )


def test_subsidiary_filing_names_the_payer():
    header = (
        '<table id="XFormD2_Form0_Table0"><tbody><tr>'
        + cell("자회사인")
        + cell("SK텔레콤(주)", True)
        + cell("의 주요경영사항신고")
        + "</tr></tbody></table>"
    )
    decision = parse_dividend_document(
        "20240205800007", page(header, main_table(common="1,050"))
    )
    assert decision.subsidiary == "SK텔레콤(주)"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"common": "약 100"}, "unreadable amount"),
        ({"record": "2023.12.31"}, "unreadable date"),
    ],
)
def test_unreadable_values_are_refused(change, message):
    with pytest.raises(SpecUndefinedError, match=message):
        parse_dividend_document("20240207801246", page(main_table(**change)))


def test_missing_table_and_utf8_despite_euc_kr_meta(tmp_path):
    with pytest.raises(SpecUndefinedError, match="dividend table not found"):
        parse_dividend_document("20240207801246", page("<p>본문</p>"))
    raw = page(main_table()).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("20240207801246.xml", raw)
    assert read_document(buffer.getvalue()) == raw.decode("utf-8")
    path = tmp_path / "20240207801246.zip"
    path.write_bytes(buffer.getvalue())
    assert load_documents([path])[0].per_share_common == D(105)
    with pytest.raises(SpecUndefinedError, match="neither UTF-8 nor declared"):
        read_document("배당".encode("euc-kr"))


def test_documents_before_2023_are_real_euc_kr():
    # 2019-2022 filings (e.g. 20191230900191) declare euc-kr and are cp949.
    raw = page(main_table()).encode("cp949")
    assert raw.decode("utf-8", "replace") != raw.decode("cp949")
    decision = parse_dividend_document("20191230900191", read_document(raw))
    assert decision.per_share_common == D(105)
    undeclared = page(main_table()).replace("charset=euc-kr", "").encode("cp949")
    with pytest.raises(SpecUndefinedError, match="neither UTF-8 nor declared"):
        read_document(undeclared)


def test_ex_dividend_date_matches_kt_and_g_2023():
    sessions = (
        date(2023, 12, 26),
        date(2023, 12, 27),
        date(2023, 12, 28),
        date(2024, 1, 2),
    )
    assert ex_dividend_date(date(2023, 12, 31), sessions, date(2024, 1, 2)) == date(
        2023, 12, 27
    )
    # Record date on a session: the session before it.
    assert ex_dividend_date(date(2023, 12, 28), sessions, date(2024, 1, 2)) == date(
        2023, 12, 27
    )
    with pytest.raises(SpecUndefinedError, match="not confirmed"):
        ex_dividend_date(date(2023, 12, 31), sessions, date(2023, 12, 28))


def test_document_fetcher_and_tool_round_trip(tmp_path, capsys):
    import importlib.util
    import json
    import sys
    from pathlib import Path
    from urllib.parse import parse_qs, urlsplit

    from momentum.krx.dart_collect import DartDocumentFetcher
    from momentum.krx.fsc_collect import CollectError

    key = "dart-test-key-0123456789"
    root = tmp_path / "raw"
    period = root / "dart_list" / "I" / "2024-01-01_2024-03-31"
    period.mkdir(parents=True)
    rows = [
        {
            "corp_code": "00126380",
            "corp_name": "JW홀딩스",
            "stock_code": "096760",
            "corp_cls": "Y",
            "report_nm": "현금ㆍ현물배당결정 ",
            "rcept_no": "20240207801246",
            "flr_nm": "x",
            "rcept_dt": "20240207",
            "rm": "유",
        },
        {
            "corp_code": "00126381",
            "corp_name": "다른회사",
            "stock_code": "000001",
            "corp_cls": "K",
            "report_nm": "주식분할결정",
            "rcept_no": "20240207801247",
            "flr_nm": "x",
            "rcept_dt": "20240207",
            "rm": "",
        },
        {
            "corp_code": "00126382",
            "corp_name": "깨진회사",
            "stock_code": "000002",
            "corp_cls": "K",
            "report_nm": "현금ㆍ현물배당결정",
            "rcept_no": "20240207801248",
            "flr_nm": "x",
            "rcept_dt": "20240207",
            "rm": "",
        },
        {
            "corp_code": "00126383",
            "corp_name": "첨부회사",
            "stock_code": "000003",
            "corp_cls": "K",
            "report_nm": "[첨부정정]현금ㆍ현물배당결정",
            "rcept_no": "20240207801249",
            "flr_nm": "x",
            "rcept_dt": "20240207",
            "rm": "",
        },
    ]
    (period / "page-1.json").write_text(
        json.dumps({"status": "000", "list": rows}), encoding="utf-8"
    )
    (period / "fetch.json").write_text(json.dumps({"pages": [{"file": "page-1.json"}]}))

    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("20240207801246.xml", page(main_table()).encode("utf-8"))
    calls = []

    def dart(url):
        query = parse_qs(urlsplit(url).query)
        assert query["crtfc_key"] == [key]
        calls.append(query["rcept_no"][0])
        if query["rcept_no"][0] == "20240207801246":
            return 200, good.getvalue()
        return (
            200,
            b'<?xml version="1.0" encoding="UTF-8"?><result><status>014</status></result>',
        )

    spec = importlib.util.spec_from_file_location(
        "dart_dividends_tool",
        Path(__file__).resolve().parents[2] / "tools/dart_dividends.py",
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    out = tmp_path / "out"
    code = tool.main(
        ["--dart-root", str(root), "--out", str(out)],
        environ={"OPENDART_API_KEY": key},
        http=dart,
        sleep=lambda s: None,
    )
    assert code == 1  # one document could not be fetched
    assert capsys.readouterr().out.strip() == (
        "dart-dividends: 3 filings, 1 parsed (0 subsidiary, 1 without pay date), 1 skipped, 1 problems"
    )
    (record,) = [
        json.loads(line) for line in (out / "dividends.jsonl").read_text().splitlines()
    ]
    assert record["stock_code"] == "096760" and record["per_share_common"] == "105"
    assert record["available_at"].startswith("2024-02-07T18:00")
    report = json.loads((out / "dividend-report.json").read_text())
    assert report["problems"][0]["rcept_no"] == "20240207801248"
    assert sorted(calls) == ["20240207801246", "20240207801248", "20240207801249"]
    assert report["skipped"] == [
        {
            "rcept_no": "20240207801249",
            "reason": "attachment correction without document",
        }
    ]
    # Saved documents are not fetched again; errors were never saved.
    fetcher = DartDocumentFetcher(
        root, environ={"OPENDART_API_KEY": key}, http=dart, sleep=lambda s: None
    )
    assert fetcher.fetch("20240207801246") == "skipped"
    assert not fetcher.path("20240207801248").exists()
    with pytest.raises(CollectError, match="invalid rcept_no"):
        fetcher.fetch("123")


def test_deferred_record_date_is_kept_as_unknown():
    # 2024 H1: SK, DB손해보험, 삼성화재, 삼성카드 left 배당기준일 as "-" and set it later.
    decision = parse_dividend_document("20240207801399", page(main_table(record="-")))
    assert decision.record_date is None
