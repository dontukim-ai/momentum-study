"""tools/jump_evidence.py on synthetic inputs (no real data)."""

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("jump_evidence", ROOT / "tools/jump_evidence.py")
je = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = je
spec.loader.exec_module(je)


def test_labels_by_hand():
    assert je.label(5.0, 1_000, 200) == "share_change_consistent"        # 5:1 reverse split, value carried
    assert je.label(0.2, 1_000, 3_000) == "share_change_other"           # x0.2 price on x3 shares: value x0.6
    assert je.label(8.8, 1_000, 1_000) == "no_share_change"
    assert je.label(8.8, None, 1_000) == "no_share_data"


def test_each_jump_gets_its_share_counts_and_the_named_filings_in_its_window():
    n = 40
    sessions = t.weekdays(n)
    paths = {"024810": [1000.0] * 20 + [8800.0] * 20,       # x8.8 with the share count cut 1/10: a reduction
             "017670": [50000.0] * 20 + [8650.0] * 20,      # x0.173 with shares x3.04: spin-off and split
             "000660": [100.0] * 20 + [400.0] * 20}         # x4, same count, no filing: data error or relisting
    data, _ = t.make_data(paths, n=n)
    jump = sessions[20]
    counts = {"024810": (10_000, 1_000), "017670": (72_000, 218_800), "000660": (500, 500)}

    def shares_at(symbol, day):
        before, after = counts[symbol]
        return after if day >= jump else before

    def filing(code, days_before, title):
        return SimpleNamespace(stock_code=code, rcept_date=jump - timedelta(days=days_before), report_nm=title)

    events = [filing("024810", 30, "주요사항보고서(감자결정)"),
              filing("017670", 150, "주요사항보고서(회사분할결정)"),
              filing("017670", 400, "주요사항보고서(회사분할결정)"),        # too early for the window
              filing("000660", 10, "분기보고서"),                         # not a share-changing title
              filing("005930", 10, "주요사항보고서(감자결정)")]           # another stock
    result = je.evidence(data, shares_at, events)
    by = {r["symbol"]: r for r in result["jumps"]}
    assert by["024810"]["label"] == "share_change_consistent" and by["024810"]["share_ratio"] == 0.1
    assert [f["title"] for f in by["024810"]["filings"]] == ["주요사항보고서(감자결정)"]
    assert by["017670"]["label"] == "share_change_other" and len(by["017670"]["filings"]) == 1
    assert by["000660"]["label"] == "no_share_change" and by["000660"]["filings"] == []
    assert result["by_label"] == {"no_share_change": 1, "share_change_consistent +filing": 1,
                                  "share_change_other +filing": 1}
    assert "024810" in je.text(result) and date(2020, 1, 1) <= jump
