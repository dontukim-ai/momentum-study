"""tools/drop_study.py (v29) on synthetic inputs, computed by hand."""

import importlib.util
import json
import math
import statistics
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("drop_study", ROOT / "tools/drop_study.py")
ds = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ds
spec.loader.exec_module(ds)

N, K = 220, 150


def gap_down(n=N, k=K, to=85.0):
    """Wiggles 1% around 100, gaps to ``to`` on k and wiggles there; a rebound to 90 from k+3."""
    out = []
    for j in range(n):
        base = 100.0 if j < k else (to if j < k + 3 else 90.0)
        out.append(base * (1.005 if j % 2 else 0.995))
    return out


def test_drop_is_the_excess_move_over_its_own_five_session_volatility_by_hand():
    stock = [100.0 * (1.01 if j % 2 else 1.0) for j in range(65)] + [80.0]       # 66 closes
    etf = [1.0] * 66
    move, vol = ds.drop(stock, etf)
    daily = [stock[j] / stock[j - 1] - 1 for j in range(1, 61)]
    assert move == pytest.approx(80.0 / stock[60] - 1)
    assert vol == pytest.approx(statistics.stdev(daily) * math.sqrt(5))
    assert ds.hit(stock, etf) and not ds.hit(stock[:-1] + [stock[60]], etf)


def test_one_event_on_the_gap_day_and_a_filing_from_k_minus_5_to_k_makes_it_news():
    data, sessions = t.make_data({"005930": gap_down(), "069500": [10000] * N}, n=N)
    found = ds.events(data)
    assert [(e["symbol"], e["k"], e["signals"]) for e in found] == [("005930", K, ["drop_nonews"])]
    for day, group in ((sessions[K - 6], "drop_nonews"), (sessions[K - 5], "drop_news"), (sessions[K], "drop_news")):
        assert ds.events(data, filings={"005930": [day]})[0]["signals"] == [group]
    rows, _ = ds.ps.paths(data, found)
    s = ds.summary(rows, sessions[0], sessions[-1])
    assert s["h5_cohort_mean"] == pytest.approx(rows[0]["spans"][4] - 0.0073)


def test_filing_days_read_rcept_dt_of_listed_rows(tmp_path):
    page = tmp_path / "p.json"
    page.write_text(json.dumps({"list": [{"stock_code": "005930", "rcept_dt": "20240105"},
                                         {"stock_code": " ", "rcept_dt": "20240105"},
                                         {"stock_code": "005930", "rcept_dt": "20240103"}]}), encoding="utf-8")
    days = ds.filing_days([page])
    assert [d.isoformat() for d in days["005930"]] == ["2024-01-03", "2024-01-05"] and list(days) == ["005930"]
    assert ds.filed(days["005930"], days["005930"][0], days["005930"][0])
    assert not ds.filed(days["005930"], date(2024, 1, 6), date(2024, 1, 8))


def test_a_quarantined_stock_has_no_event_and_only_the_trial_is_judged(tmp_path):
    data, sessions = t.make_data({"005930": gap_down(), "069500": [10000] * N}, n=N)
    assert ds.events(data, t._events(("005930", "restructuring", sessions[K - 30]))) == []
    result = ds.study(data)
    assert result["signals"]["drop_nonews"]["edge_h5"] is False                  # one cohort: no t
    assert "edge_h5" not in result["signals"]["drop_news"]
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        ds.main(["--input", str(tmp_path), "--events", "e", "--dart-root", str(tmp_path), "--record", str(record)])
