"""FSC quote pages -> price CSV rows (synthetic pages, offline)."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from momentum.agent.market import KRX
from momentum.agent.quality import screen_price_rows
from momentum.config import SpecUndefinedError
from momentum.krx.fsc_prices import convert_pages, identity_changes


def item(day="20240603", code="000001", isin="KR7000001000", **prices):
    values = {
        "mkp": "1000",
        "hipr": "1100",
        "lopr": "990",
        "clpr": "1050",
        "trqu": "500",
    }
    values.update(prices)
    return {
        "basDt": day,
        "srtnCd": code,
        "isinCd": isin,
        "itmsNm": f"종목{code}",
        **values,
    }


def page(tmp_path, name, items, *, page_no=1, total=None, code="00"):
    body = {
        "numOfRows": 1000,
        "pageNo": page_no,
        "totalCount": len(items) if total is None else total,
        "items": {"item": items} if items else "",
    }
    path = tmp_path / name
    path.write_text(
        json.dumps({"response": {"header": {"resultCode": code}, "body": body}}),
        encoding="utf-8",
    )
    return path


def test_rows_follow_csv_contract_and_information_clock(tmp_path):
    result = convert_pages(
        [page(tmp_path, "p1.json", [item(), item(code="0000D0", isin="KR70000D0009")])],
        "etf",
    )
    first = result.rows[0]
    assert first["session"] == "2024-06-03" and first["symbol"] == "000001"
    assert (
        first["open"],
        first["high"],
        first["low"],
        first["close"],
        first["volume"],
    ) == (
        "1000",
        "1100",
        "990",
        "1050",
        "500",
    )
    assert first["available_at"] == KRX.historical_bar_at(date(2024, 6, 3)).isoformat()
    assert (
        first["source"].startswith("fsc-etf-price:") and len(first["source"]) == 14 + 64
    )
    assert [i.isin for i in result.identities] == ["KR7000001000", "KR70000D0009"]
    assert result.pages[0]["rows"] == 2
    assert not screen_price_rows(result.rows).quarantined


def test_missing_or_zero_prices_are_copied_for_quarantine_not_filled(tmp_path):
    rows = convert_pages(
        [
            page(
                tmp_path,
                "p.json",
                [item(mkp="0"), item(code="000002", isin="KR7000002008", mkp=None)],
            )
        ],
        "stock",
    ).rows
    assert rows[0]["open"] == "0" and rows[1]["open"] == ""
    screen = screen_price_rows(rows)
    assert screen.accepted == []
    assert screen.by_code() == {"NONPOSITIVE_PRICE": 1, "PARSE_ERROR": 1}


def test_multi_page_session_must_be_complete(tmp_path):
    one = page(tmp_path, "p1.json", [item()], page_no=1, total=2)
    with pytest.raises(SpecUndefinedError, match="1 of 2 rows"):
        convert_pages([one], "stock")
    two = page(
        tmp_path,
        "p2.json",
        [item(code="000002", isin="KR7000002008")],
        page_no=2,
        total=2,
    )
    assert len(convert_pages([one, two], "stock").rows) == 2
    with pytest.raises(SpecUndefinedError, match="repeated page"):
        convert_pages(
            [one, page(tmp_path, "p3.json", [item()], page_no=1, total=2)], "stock"
        )


def test_supplier_errors_and_types_are_refused(tmp_path):
    with pytest.raises(SpecUndefinedError, match="resultCode"):
        convert_pages([page(tmp_path, "e.json", [item()], code="30")], "stock")
    with pytest.raises(SpecUndefinedError, match="unsupported JSON type"):
        convert_pages([page(tmp_path, "f.json", [item(clpr=1050.5)])], "stock")
    with pytest.raises(SpecUndefinedError, match="one basDt"):
        convert_pages(
            [page(tmp_path, "d.json", [item(), item(day="20240604")])], "stock"
        )
    with pytest.raises(SpecUndefinedError, match="two ISINs"):
        convert_pages(
            [page(tmp_path, "i.json", [item(), item(isin="KR7999999007")])], "stock"
        )
    with pytest.raises(SpecUndefinedError, match="unknown FSC price kind"):
        convert_pages([], "bond")


def test_single_item_object_and_empty_day(tmp_path):
    path = tmp_path / "single.json"
    path.write_text(
        json.dumps(
            {
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {"pageNo": 1, "totalCount": 1, "items": {"item": item()}},
                }
            }
        ),
        encoding="utf-8",
    )
    assert len(convert_pages([path], "stock").rows) == 1
    empty = convert_pages([page(tmp_path, "empty.json", [], total=0)], "stock")
    assert empty.rows == [] and empty.pages[0]["rows"] == 0


def test_identity_changes_are_reported_not_merged(tmp_path):
    result = convert_pages(
        [
            page(tmp_path, "a.json", [item(day="20240603")]),
            page(tmp_path, "b.json", [item(day="20240604", code="000009")]),
            page(tmp_path, "c.json", [item(day="20240605", isin="KR7000003006")]),
        ],
        "stock",
    )
    kinds = [change["kind"] for change in identity_changes(result.identities)]
    assert kinds == ["SYMBOL_CHANGED", "SYMBOL_REUSED"]


def test_cli_writes_outputs_and_reports_screen(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location(
        "fsc_prices_tool", Path(__file__).resolve().parents[2] / "tools/fsc_prices.py"
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    pages = [
        page(
            tmp_path,
            "p.json",
            [item(), item(code="000002", isin="KR7000002008", lopr="1200")],
        )
    ]
    out = tmp_path / "out"
    assert (
        tool.main(["--kind", "stock", "--pages", *map(str, pages), "--out", str(out)])
        == 0
    )
    assert capsys.readouterr().out.strip() == (
        "fsc-prices: 2 rows, 1 accepted, 1 quarantined, 2 identities"
    )
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["screen"]["quarantined"] == {"OHLC_INCONSISTENT": 1}
    assert (out / "prices.csv").read_text(encoding="utf-8").splitlines()[0] == (
        "session,symbol,open,high,low,close,volume,available_at,source"
    )
    assert len((out / "identities.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert (
        tool.main(
            [
                "--kind",
                "stock",
                "--pages",
                str(page(tmp_path, "bad.json", [item()], code="99")),
                "--out",
                str(out),
            ]
        )
        == 1
    )
