"""momentum.krx.market_events: liquidation and restructuring filings, point in time."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from momentum.config import SpecUndefinedError
from momentum.krx import market_events as me

KST = ZoneInfo("Asia/Seoul")


def filing(code, day, title):
    return SimpleNamespace(stock_code=code, rcept_date=day, report_nm=title,
                           available_at=datetime.combine(day, datetime.min.time(), KST).replace(hour=18))


def test_titles_are_classified_and_subsidiary_filings_are_not_the_stock():
    assert me.classify("주권매매거래정지해제(상장폐지에 따른 정리매매 개시)") == "liquidation"
    assert me.classify("주요사항보고서(회사합병결정)") == "restructuring"
    assert me.classify("[기재정정]주요사항보고서(회사분할결정)") == "restructuring"
    assert me.classify("주요사항보고서(회사분할합병결정)") == "restructuring"
    assert me.classify("주요사항보고서(주식교환ㆍ이전결정)") == "restructuring"
    assert me.classify("회사합병결정(종속회사의주요경영사항)") is None
    assert me.classify("주식분할결정") is None                     # a split is a share action, recorded elsewhere
    assert me.classify("주요사항보고서(감자결정)") is None


def test_liquidation_window_and_quarantine_are_point_in_time():
    d = date(2024, 3, 4)
    events = me.MarketEvents(me.build([filing("111110", d, "주권매매거래정지해제(상장폐지에 따른 정리매매 개시)"),
                                       filing("222220", d, "주요사항보고서(회사합병결정)"),
                                       filing(None, d, "주요사항보고서(회사합병결정)")]))
    assert events.liquidating("111110", d) and events.liquidating("111110", d + timedelta(days=me.LIQUIDATION_DAYS))
    assert not events.liquidating("111110", d - timedelta(days=1))
    assert not events.liquidating("111110", d + timedelta(days=me.LIQUIDATION_DAYS + 1))
    at = datetime.combine(d, datetime.min.time(), KST)
    assert not events.quarantined("222220", at.replace(hour=17))     # filed that day, available from 18:00
    assert events.quarantined("222220", at.replace(hour=18))
    assert events.quarantined("222220", at.replace(hour=18) + timedelta(days=me.QUARANTINE_DAYS))
    assert not events.quarantined("222220", at.replace(hour=18) + timedelta(days=me.QUARANTINE_DAYS + 1))
    assert not events.quarantined("111110", at.replace(hour=18))
    assert not me.MarketEvents([]).liquidating("111110", d)


def test_events_file_round_trips_and_is_written_once(tmp_path):
    rows = me.build([filing("222220", date(2024, 3, 4), "주요사항보고서(회사합병결정)")])
    path = tmp_path / "market-events.json"
    me.write(rows, path)
    assert me.MarketEvents.load(path).quarantined("222220", datetime(2024, 3, 5, tzinfo=KST))
    with pytest.raises(SpecUndefinedError):
        me.write(rows, path)


def test_the_tool_builds_from_saved_b_and_i_pages(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("market_events_tool", root / "tools/market_events.py")
    tool = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(tool)
    rows = [{"rcept_no": "20240304000123", "rcept_dt": "20240304", "corp_code": "00123456", "corp_cls": "K",
             "stock_code": "222220", "corp_name": "가", "report_nm": "주요사항보고서(회사합병결정)", "rm": ""}]
    for kind in ("B", "I"):
        period = tmp_path / "dart_list" / kind / "2024-01-01_2024-03-31"
        period.mkdir(parents=True)
        (period / "page-1.json").write_text(json.dumps({"status": "000", "list": rows if kind == "B" else []}))
        (period / "fetch.json").write_text(json.dumps({"pages": [{"file": "page-1.json"}]}))
    out = tmp_path / "market-events.json"
    assert tool.main(["--dart-root", str(tmp_path), "--out", str(out)]) == 0
    assert [r["symbol"] for r in json.loads(out.read_text())] == ["222220"]
