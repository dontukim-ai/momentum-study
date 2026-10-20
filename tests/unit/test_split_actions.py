"""DART list collector (fake transport) and split reconciliation, offline."""

import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from test_fsc_adjustments import ECO, quote, split_fixture, write_day

from momentum.agent.cli import read_input
from momentum.agent.market import KRX
from momentum.krx.dart_collect import DartListCollector, list_pages, periods
from momentum.krx.dart_events import parse_list_pages
from momentum.krx.fsc_adjustments import detect
from momentum.krx.split_actions import reconcile

KEY = "dart-test-key-0123456789"
ENV = {"OPENDART_API_KEY": KEY}


def filing(rcept_no, code, report, corp="00126380"):
    return {
        "corp_code": corp,
        "corp_name": "회사",
        "stock_code": code,
        "corp_cls": "K",
        "report_nm": report,
        "rcept_no": rcept_no,
        "flr_nm": "회사",
        "rcept_dt": rcept_no[:8],
        "rm": "",
    }


class Dart:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, url):
        query = parse_qs(urlsplit(url).query)
        assert query["crtfc_key"] == [KEY] and query["pblntf_ty"] == ["I"]
        page = int(query["page_no"][0])
        self.calls.append((query["bgn_de"][0], page))
        bgn, end = query["bgn_de"][0], query["end_de"][0]
        rows = [r for r in self.rows if bgn <= r["rcept_dt"] <= end]
        if not rows:
            return 200, json.dumps({"status": "013", "message": "no data"}).encode()
        size = int(query["page_count"][0])
        chunk = rows[(page - 1) * size : page * size]
        return 200, json.dumps(
            {
                "status": "000",
                "page_no": page,
                "total_count": len(rows),
                "total_page": -(-len(rows) // size),
                "list": chunk,
            }
        ).encode()


def collector(root, dart):
    return DartListCollector(
        root,
        "I",
        environ=ENV,
        http=dart,
        sleep=lambda s: None,
        clock=lambda: datetime(2026, 10, 19, tzinfo=UTC),
    )


def test_periods_respect_92_days():
    spans = list(periods(date(2024, 1, 1), date(2024, 6, 30)))
    assert spans[0] == (date(2024, 1, 1), date(2024, 4, 1)) and spans[-1][1] == date(
        2024, 6, 30
    )
    assert all((b - a).days < 92 for a, b in spans)


def test_periods_respect_three_calendar_months():
    # OpenDART probed 2026-10-20: the end may reach start + 3 months (day
    # clamped to month end) and no further; 92 days crosses it over February.
    spans = list(periods(date(2021, 1, 3), date(2021, 7, 31)))
    assert spans[0] == (date(2021, 1, 3), date(2021, 4, 3))
    assert next(periods(date(2020, 11, 30), date(2021, 3, 31))) == (
        date(2020, 11, 30),
        date(2021, 2, 28),
    )
    # The API would take 2020-10-31..2021-01-31 (93 days); 92 days is tighter.
    assert next(periods(date(2020, 10, 31), date(2021, 3, 31))) == (
        date(2020, 10, 31),
        date(2021, 1, 30),
    )
    # Spans that already passed keep their boundaries, so saved periods
    # are found again on rerun.
    assert list(periods(date(2019, 10, 1), date(2021, 1, 2))) == [
        (date(2019, 10, 1), date(2019, 12, 31)),
        (date(2020, 1, 1), date(2020, 4, 1)),
        (date(2020, 4, 2), date(2020, 7, 2)),
        (date(2020, 7, 3), date(2020, 10, 2)),
        (date(2020, 10, 3), date(2021, 1, 2)),
    ]


def test_collects_pages_resumably_and_handles_empty_periods(tmp_path):
    rows = [
        filing(f"20240{3 + (n % 2)}01{n:06d}", "086520", "기타") for n in range(150)
    ]
    dart = Dart(rows)
    results = collector(tmp_path, dart).collect(date(2024, 1, 1), date(2024, 6, 30))
    assert [r.status for r in results] == ["fetched", "fetched"]
    assert sum(r.total_count for r in results) == 150
    pages = list_pages(tmp_path, "I")
    assert len(parse_list_pages(pages)) == 150
    record = json.loads((pages[0].parent / "fetch.json").read_text())
    assert KEY not in json.dumps(record) and record["pblntf_ty"] == "I"
    calls = len(dart.calls)
    again = collector(tmp_path, dart).collect(date(2024, 1, 1), date(2024, 6, 30))
    assert [r.status for r in again] == ["skipped", "skipped"] and len(
        dart.calls
    ) == calls
    empty = collector(tmp_path / "e", Dart([])).collect(
        date(2024, 1, 1), date(2024, 1, 31)
    )
    assert [(r.status, r.total_count) for r in empty] == [("fetched", 0)]


def events(tmp_path, rows):
    page = tmp_path / "list.json"
    page.write_text(json.dumps({"status": "000", "list": rows}), encoding="utf-8")
    return parse_list_pages([page])


def test_split_reconciles_with_prior_original_filing(tmp_path):
    split_fixture(tmp_path / "raw")
    found = detect(tmp_path / "raw", date(2024, 4, 5), date(2024, 4, 11))
    filed = events(
        tmp_path,
        [
            filing("20240328000100", "086520", "[기재정정]주식분할결정"),
            filing("20240301000100", "086520", "주식분할결정"),
        ],
    )
    result = reconcile(found, filed)
    assert result.unresolved == []
    (action,) = result.actions
    assert action["security_id"] == "086520" and action["ex_date"] == "2024-04-11"
    assert action["split_ratio"] == "5"
    assert (
        action["available_at"]
        == datetime(2024, 3, 1, 18, tzinfo=KRX.timezone).isoformat()
    )
    assert action["evidence"]["rcept_no"] == "20240301000100"


def test_late_missing_and_non_terminating_ratios_are_unresolved(tmp_path):
    split_fixture(tmp_path / "raw")
    found = detect(tmp_path / "raw", date(2024, 4, 5), date(2024, 4, 11))
    late = reconcile(
        found, events(tmp_path, [filing("20240411000100", "086520", "주식분할결정")])
    )
    assert (
        late.actions == []
        and late.unresolved[0]["reason"] == "disclosure filed after the open"
    )
    none = reconcile(
        found, events(tmp_path, [filing("20240301000100", "000001", "주식분할결정")])
    )
    assert none.unresolved[0]["reason"] == "no matching disclosure"
    write_day(tmp_path / "third", "2024-06-03", [quote(ECO, 900, 0, 1000)])
    write_day(tmp_path / "third", "2024-06-04", [quote(ECO, 310, 10, 3000)])
    third = detect(tmp_path / "third", date(2024, 6, 3), date(2024, 6, 4))
    assert third[0].ratio == "3"
    write_day(tmp_path / "rev", "2024-06-03", [quote(ECO, 100, 0, 3000)])
    write_day(tmp_path / "rev", "2024-06-04", [quote(ECO, 310, 10, 1000)])
    reverse = detect(tmp_path / "rev", date(2024, 6, 3), date(2024, 6, 4))
    result = reconcile(
        reverse, events(tmp_path, [filing("20240501000100", "086520", "주식병합결정")])
    )
    assert result.unresolved[0]["reason"] == "ratio is not a finite decimal"


def test_actions_file_loads_as_research_input(tmp_path):
    split_fixture(tmp_path / "raw")
    found = detect(tmp_path / "raw", date(2024, 4, 5), date(2024, 4, 11))
    actions = reconcile(
        found, events(tmp_path, [filing("20240301000100", "086520", "주식분할결정")])
    ).actions
    path = tmp_path / "actions.json"
    path.write_text(json.dumps(actions))
    prices = tmp_path / "prices.csv"
    prices.write_text(
        "session,symbol,open,high,low,close,volume,available_at,source\n"
        "2024-04-11,086520,108100,108100,108100,108100,1,2024-04-11T09:00:00+00:00,t\n"
    )
    securities = tmp_path / "securities.jsonl"
    securities.write_text(
        json.dumps(
            {
                "symbol": "086520",
                "effective_at": "2024-01-01T00:00:00+00:00",
                "available_at": "2024-01-01T00:00:00+00:00",
                "status": "NORMAL",
                "asset_type": "STOCK",
                "market": "KOSDAQ",
                "managed": False,
                "caution": False,
                "leveraged": False,
                "inverse": False,
                "bid": None,
                "ask": None,
                "source": "test",
            }
        )
        + "\n"
    )
    data = read_input(prices, securities, path)
    (announced,) = data.actions
    assert announced.action.split_ratio == D(5) and announced.action.ex_date == date(
        2024, 4, 11
    )


def test_cli_round_trip(tmp_path, capsys):
    root = tmp_path / "raw"
    split_fixture(root / "fsc")
    collector(root, Dart([filing("20240301000100", "086520", "주식분할결정")])).collect(
        date(2024, 3, 1), date(2024, 4, 11)
    )

    def load(name):
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().parents[2] / f"tools/{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    tool = load("split_actions")
    out = tmp_path / "out"
    assert (
        tool.main(
            [
                "--fsc-root",
                str(root / "fsc"),
                "--dart-root",
                str(root),
                "--start",
                "2024-04-05",
                "--end",
                "2024-04-11",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "split-actions: 1 actions, 0 unresolved"
    assert json.loads((out / "actions.json").read_text())[0]["split_ratio"] == "5"
    collect = load("dart_collect")
    assert (
        collect.main(
            [
                "--start",
                "2024-03-01",
                "--end",
                "2024-03-02",
                "--root",
                str(tmp_path / "x"),
            ],
            environ={},
        )
        == 1
    )


def test_preferred_class_inherits_common_split_of_same_issuer(tmp_path):
    common, preferred, stranger = "KR7001460005", "KR7001461003", "KR7009990001"
    write_day(tmp_path / "raw", "2024-04-08", [
        quote(common, 489500, 0, 1000, code="001460"),
        quote(preferred, 198900, 0, 100, code="001465"),
        quote(stranger, 5000, 0, 10, code="009990"),
    ])
    write_day(tmp_path / "raw", "2024-04-09", [
        quote(common, 49450, 500, 10000, code="001460"),
        quote(preferred, 19990, 100, 1000, code="001465"),
        quote(stranger, 600, 100, 100, code="009990"),
    ])
    found = detect(tmp_path / "raw", date(2024, 4, 8), date(2024, 4, 9))
    result = reconcile(found, events(tmp_path, [filing("20240304000100", "001460", "주식분할결정")]))
    by_code = {a["security_id"]: a for a in result.actions}
    assert set(by_code) == {"001460", "001465"}
    assert by_code["001465"]["available_at"] == by_code["001460"]["available_at"]
    assert by_code["001465"]["evidence"]["via_share_class"] == "001460"
    assert by_code["001465"]["evidence"]["isin"] == preferred
    # Another issuer with no filing is not adopted.
    assert [u["symbol"] for u in result.unresolved] == ["009990"]
