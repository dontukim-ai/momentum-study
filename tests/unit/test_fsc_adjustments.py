"""Base-price adjustment detection on synthetic collected days."""

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from momentum.krx.fsc_adjustments import detect


def quote(isin, close, change, shares, *, code="086520", opening=None, volume="100"):
    opening = str(close) if opening is None else opening
    return {
        "basDt": "",
        "srtnCd": code,
        "isinCd": isin,
        "clpr": str(close),
        "vs": str(change),
        "lstgStCnt": str(shares),
        "mkp": opening,
        "hipr": str(close),
        "lopr": str(close),
        "trqu": volume,
    }


def write_day(root, day, rows):
    folder = root / "stock" / day
    folder.mkdir(parents=True)
    for row in rows:
        row["basDt"] = day.replace("-", "")
    body = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "pageNo": 1,
                "totalCount": len(rows),
                "items": {"item": rows} if rows else "",
            },
        }
    }
    (folder / "page-1.json").write_text(json.dumps(body))
    (folder / "fetch.json").write_text(
        json.dumps(
            {
                "requested_date": day,
                "total_count": len(rows),
                "pages": [{"file": "page-1.json", "rows": len(rows)}],
            }
        )
    )


ECO = "KR7086520004"
OTHER = "KR7000001000"


def split_fixture(root):
    # Shape of the real 2024-04 EcoPro 1:5 split: halt, then a base of 517,000/5.
    write_day(
        root,
        "2024-04-05",
        [
            quote(ECO, 550000, -14000, 26627668),
            quote(OTHER, 1000, 0, 10, code="000001"),
        ],
    )
    write_day(
        root,
        "2024-04-08",
        [
            quote(ECO, 517000, -33000, 26627668),
            quote(OTHER, 1010, 10, 10, code="000001"),
        ],
    )
    write_day(
        root,
        "2024-04-09",
        [
            quote(ECO, 517000, 0, 26627668, opening="0", volume="0"),
            quote(OTHER, 1010, 0, 10, code="000001"),
        ],
    )
    write_day(
        root,
        "2024-04-10",
        [
            quote(ECO, 517000, 0, 26627668, opening="0", volume="0"),
            quote(OTHER, 1020, 10, 10, code="000001"),
        ],
    )
    write_day(
        root,
        "2024-04-11",
        [
            quote(ECO, 108100, 4700, 133138340),
            quote(OTHER, 1000, -20, 10, code="000001"),
        ],
    )


def test_split_detected_across_halt_with_factor_and_share_ratio(tmp_path):
    split_fixture(tmp_path)
    found = detect(tmp_path, date(2024, 4, 5), date(2024, 4, 11))
    assert len(found) == 1
    hit = found[0]
    assert (hit.session, hit.previous_session) == (date(2024, 4, 11), date(2024, 4, 8))
    assert (hit.previous_close, hit.adjusted_base) == (D(517000), D(103400))
    assert hit.factor == D("5.000000") and hit.share_ratio == D("5.000000")
    assert hit.kind == "SPLIT_LIKE"


def test_reverse_split_and_unexplained_base_change(tmp_path):
    write_day(
        tmp_path,
        "2024-06-03",
        [quote(ECO, 1000, 0, 1000), quote(OTHER, 2000, 0, 50, code="000001")],
    )
    write_day(
        tmp_path,
        "2024-06-04",
        [quote(ECO, 10100, 100, 100), quote(OTHER, 1800, -100, 50, code="000001")],
    )
    kinds = {
        a.isin: a.kind for a in detect(tmp_path, date(2024, 6, 3), date(2024, 6, 4))
    }
    assert kinds == {ECO: "REVERSE_SPLIT_LIKE", OTHER: "OTHER"}


def test_gap_breaks_comparison(tmp_path):
    write_day(tmp_path, "2024-06-03", [quote(ECO, 1000, 0, 1000)])
    write_day(tmp_path, "2024-06-05", [quote(ECO, 210, 10, 5000)])
    assert detect(tmp_path, date(2024, 6, 3), date(2024, 6, 5)) == []


def test_cli_writes_jsonl(tmp_path, capsys):
    split_fixture(tmp_path / "raw")
    spec = importlib.util.spec_from_file_location(
        "fsc_adjustments_tool",
        Path(__file__).resolve().parents[2] / "tools/fsc_adjustments.py",
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    out = tmp_path / "adj.jsonl"
    assert (
        tool.main(
            [
                "--root",
                str(tmp_path / "raw"),
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
    assert (
        capsys.readouterr().out.strip()
        == "fsc-adjustments: 1 adjustments (SPLIT_LIKE 1)"
    )
    record = json.loads(out.read_text().splitlines()[0])
    assert record["factor"] == "5.000000" and record["session"] == "2024-04-11"
