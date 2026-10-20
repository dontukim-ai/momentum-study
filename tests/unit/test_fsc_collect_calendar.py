"""FSC collector (fake transport) and observed calendar, offline."""

import importlib.util
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from momentum.config import SpecUndefinedError
from momentum.krx.fsc_calendar import build_calendar
from momentum.krx.fsc_collect import (
    ENDPOINTS,
    PAGE_ROWS,
    CollectError,
    Collector,
    weekdays,
)
from momentum.krx.fsc_prices import convert_pages

KEY = "fake+key/with=chars"
ENV = {"DATA_GO_KR_SERVICE_KEY": KEY}


def body(day, rows, total, page=1, code="00"):
    items = [
        {
            "basDt": day,
            "srtnCd": f"{n:06d}",
            "isinCd": f"KR7{n:08d}0",
            "itmsNm": "x",
            "mkp": "100",
            "hipr": "110",
            "lopr": "90",
            "clpr": "105",
            "trqu": "7",
        }
        for n in range(rows)
    ]
    payload = {
        "response": {
            "header": {"resultCode": code},
            "body": {
                "pageNo": page,
                "totalCount": total,
                "items": {"item": items} if items else "",
            },
        }
    }
    return json.dumps(payload).encode()


class Supplier:
    """Fake data.go.kr: rows per date, paginated at PAGE_ROWS."""

    def __init__(self, rows_by_day, fail=()):
        self.rows, self.fail, self.calls = rows_by_day, set(fail), []

    def __call__(self, url):
        query = parse_qs(urlsplit(url).query)
        assert query["serviceKey"] == [KEY] and urlsplit(url).path.endswith("_V2")
        day, page = query["basDt"][0], int(query["pageNo"][0])
        self.calls.append((day, page))
        if (day, page) in self.fail:
            raise URLError("down")
        total = self.rows.get(day, 0)
        count = max(0, min(PAGE_ROWS, total - (page - 1) * PAGE_ROWS))
        return 200, body(day, count, total, page)


def collector(tmp_path, supplier, **kw):
    return Collector(
        tmp_path / "raw",
        "stock",
        environ=ENV,
        http=supplier,
        sleep=lambda s: None,
        clock=lambda: datetime(2026, 10, 19, tzinfo=UTC),
        **kw,
    )


def test_weekdays_skip_weekends():
    assert [d.day for d in weekdays(date(2024, 6, 1), date(2024, 6, 4))] == [3, 4]


def test_collects_all_pages_atomically_and_skips_completed_days(tmp_path):
    supplier = Supplier({"20240603": 2500, "20240604": 3})
    results = collector(tmp_path, supplier).collect(date(2024, 6, 3), date(2024, 6, 4))
    assert [(r.status, r.total_count) for r in results] == [
        ("fetched", 2500),
        ("fetched", 3),
    ]
    day = tmp_path / "raw" / "stock" / "2024-06-03"
    record = json.loads((day / "fetch.json").read_text())
    assert record["requested_date"] == "2024-06-03" and record["total_count"] == 2500
    assert [p["rows"] for p in record["pages"]] == [1000, 1000, 500]
    assert record["endpoint"] == ENDPOINTS["stock"]
    assert KEY not in (day / "fetch.json").read_text()
    pages = sorted(day.glob("page-*.json"))
    assert len(convert_pages(pages, "stock").rows) == 2500
    calls = len(supplier.calls)
    again = collector(tmp_path, supplier).collect(date(2024, 6, 3), date(2024, 6, 4))
    assert [r.status for r in again] == ["skipped", "skipped"] and len(
        supplier.calls
    ) == calls


def test_failed_day_leaves_nothing_and_stops(tmp_path):
    supplier = Supplier({"20240603": 1500, "20240604": 1}, fail={("20240603", 2)})
    results = collector(tmp_path, supplier).collect(date(2024, 6, 3), date(2024, 6, 4))
    assert [r.status for r in results] == ["error"]
    assert results[0].message == "network retries exhausted"
    assert not (tmp_path / "raw" / "stock" / "2024-06-03").exists()
    assert not list((tmp_path / "raw" / "stock").glob(".*partial"))


def test_supplier_error_and_key_echo_are_not_saved(tmp_path):
    def denied(url):
        return 403, b'{"returnAuthMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}'

    result = collector(tmp_path, denied).collect_day(date(2024, 6, 3))
    assert result.status == "error" and "HTTP 403" in result.message
    echo = collector(tmp_path, lambda url: (200, KEY.encode())).collect_day(
        date(2024, 6, 3)
    )
    assert echo.message == "response echoes the service key; not saved"
    assert KEY not in result.message + echo.message
    with pytest.raises(CollectError, match="is not set"):
        Collector(tmp_path, "stock", environ={})


def test_empty_day_is_recorded_for_the_calendar(tmp_path):
    collector(tmp_path, Supplier({})).collect_day(date(2024, 6, 6))
    record = json.loads((tmp_path / "raw/stock/2024-06-06/fetch.json").read_text())
    assert record["total_count"] == 0 and record["pages"][0]["rows"] == 0


def test_calendar_confirms_closures_only_after_later_data(tmp_path):
    # 2024-06-06 holiday; 06-10 empty because not yet published; 06-11 not collected.
    rows = {"20240603": 5, "20240604": 5, "20240605": 5, "20240607": 5}
    collector(tmp_path, Supplier(rows)).collect(date(2024, 6, 3), date(2024, 6, 10))
    result = build_calendar(tmp_path / "raw", date(2024, 6, 3), date(2024, 6, 11))
    assert [d.day for d in result.sessions] == [3, 4, 5, 7]
    assert [d.day for d in result.closed] == [6]
    assert [d.day for d in result.pending] == [10]
    assert [d.day for d in result.uncollected] == [11]
    assert result.confirmed_through == date(2024, 6, 7)


def test_calendar_stops_at_gap_and_rejects_wrong_dates(tmp_path):
    supplier = Supplier({"20240603": 1, "20240605": 1})
    c = collector(tmp_path, supplier)
    c.collect_day(date(2024, 6, 3))
    c.collect_day(date(2024, 6, 5))
    result = build_calendar(tmp_path / "raw", date(2024, 6, 3), date(2024, 6, 5))
    assert result.sessions == (date(2024, 6, 3),) and result.uncollected == (
        date(2024, 6, 4),
    )
    assert result.confirmed_through == date(2024, 6, 3)
    page = tmp_path / "raw/stock/2024-06-05/page-1.json"
    page.write_bytes(body("20240607", 1, 1))
    with pytest.raises(SpecUndefinedError, match="another date"):
        build_calendar(tmp_path / "raw", date(2024, 6, 3), date(2024, 6, 5))


def test_empty_days_before_supplier_coverage_are_not_holidays(tmp_path):
    # FSC V2 answers 0 rows without error before its coverage starts.
    rows = {"20240605": 5, "20240607": 5}
    collector(tmp_path, Supplier(rows)).collect(date(2024, 6, 3), date(2024, 6, 7))
    result = build_calendar(tmp_path / "raw", date(2024, 6, 3), date(2024, 6, 7))
    assert [d.day for d in result.before_coverage] == [3, 4]
    assert [d.day for d in result.sessions] == [5, 7]
    assert [d.day for d in result.closed] == [6]
    assert result.report()["before_coverage"] == ["2024-06-03", "2024-06-04"]
    nothing = tmp_path / "none"
    collector(nothing, Supplier({})).collect(date(2024, 6, 3), date(2024, 6, 4))
    empty = build_calendar(nothing / "raw", date(2024, 6, 3), date(2024, 6, 4))
    assert empty.sessions == () and empty.pending == (
        date(2024, 6, 3),
        date(2024, 6, 4),
    )


def load_tool(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[2] / f"tools/{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_round_trip(tmp_path, capsys):
    collect, calendar = load_tool("fsc_collect"), load_tool("fsc_calendar")
    root = str(tmp_path / "raw")
    supplier = Supplier({"20240603": 2, "20240605": 2})
    code = collect.main(
        [
            "--kind",
            "stock",
            "--start",
            "2024-06-03",
            "--end",
            "2024-06-05",
            "--root",
            root,
        ],
        environ=ENV,
        http=supplier,
        sleep=lambda s: None,
    )
    assert code == 0
    assert (
        capsys.readouterr().out.strip() == "fsc-collect: 3 fetched, 0 skipped, 0 errors"
    )
    out = tmp_path / "cal"
    assert (
        calendar.main(
            [
                "--root",
                root,
                "--start",
                "2024-06-03",
                "--end",
                "2024-06-05",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert json.loads((out / "calendar.json").read_text()) == [
        "2024-06-03",
        "2024-06-05",
    ]
    report = json.loads((out / "calendar-report.json").read_text())
    assert (
        report["closed_weekdays"] == ["2024-06-04"]
        and report["rows_per_session"]["2024-06-03"] == 2
    )
    assert (
        collect.main(
            [
                "--kind",
                "stock",
                "--start",
                "2024-06-03",
                "--end",
                "2024-06-03",
                "--root",
                root,
            ],
            environ={},
        )
        == 1
    )
