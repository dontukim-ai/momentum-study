"""tools/ma_path_study.py (v27) on synthetic inputs, paths computed by hand."""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("ma_path_study", ROOT / "tools/ma_path_study.py")
ps = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)

N = 220
PATH = t.path_down_up_down(N)


def first_cross(path, fast, slow, start):
    for k in range(start, len(path)):
        if t.ma.cross(t.ma.sma(path[:k], fast), t.ma.sma(path[:k], slow),
                      t.ma.sma(path[:k + 1], fast), t.ma.sma(path[:k + 1], slow)) == "golden":
            return k
    raise AssertionError("no cross")


FAST_K = first_cross(PATH, 5, 20, 21)
SLOW_K = t.golden_and_dead_days(PATH, t.weekdays(N))["golden"]


def with_volume(data, sessions, symbol, k, volume):
    for b in data.snapshot.bars:
        if b.symbol == symbol and b.session == sessions[k]:
            b.volume = volume
    return data


def test_a_5_20_cross_is_an_event_and_counts_as_a_volume_cross_only_at_twice_the_20_session_mean():
    assert t.weekdays(N)[FAST_K] >= date(2020, 4, 1) and FAST_K < SLOW_K
    data, sessions = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    found, fast_days = ps.crosses(data)
    by_day = {(e["symbol"], e["k"]): e["signals"] for e in found}
    assert by_day[("005930", FAST_K)] == ["gc5_20"]
    assert by_day[("005930", SLOW_K)] == ["gc20_60"]
    assert FAST_K in fast_days["005930"]
    with_volume(data, sessions, "005930", FAST_K, 199_999_999)          # the others trade 100,000,000
    assert {(e["symbol"], e["k"]): e["signals"] for e in ps.crosses(data)[0]}[("005930", FAST_K)] == ["gc5_20"]
    with_volume(data, sessions, "005930", FAST_K, 200_000_000)
    assert {(e["symbol"], e["k"]): e["signals"] for e in ps.crosses(data)[0]}[("005930", FAST_K)] == [
        "gc5_20", "gc5_20_vol"]


def test_spans_before_and_after_and_the_h20_net_excess_by_hand():
    bench = [10000 + 10 * k for k in range(N)]
    data, _ = t.make_data({"005930": PATH, "069500": bench}, n=N)
    found = [e for e in ps.crosses(data)[0] if e["k"] == FAST_K]
    rows, left = ps.paths(data, found)
    k = FAST_K

    def excess(a, b):
        return (PATH[b] / PATH[a] - 1) - (bench[b] / bench[a] - 1)          # open = close in these bars

    expected = [excess(k - 20, k - 15), excess(k - 15, k - 10), excess(k - 10, k - 5), excess(k - 5, k),
                excess(k + 1, k + 5), excess(k + 5, k + 10), excess(k + 10, k + 15), excess(k + 15, k + 20)]
    assert rows[0]["spans"] == pytest.approx(expected)
    assert rows[0]["h20"] == pytest.approx(excess(k + 1, k + 20) - 0.0073)
    assert left == {"missing": 0, "unexplained": 0, "open_at_end": 0}


def test_a_missing_anchor_bar_before_the_cross_is_left_out_not_filled():
    data, sessions = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    found = [e for e in ps.crosses(data)[0] if e["k"] == FAST_K]
    data.snapshot.bars = [b for b in data.snapshot.bars
                          if not (b.symbol == "005930" and b.session == sessions[FAST_K - 15])]
    rows, left = ps.paths(data, found)
    assert rows == [] and left["missing"] == 1


def test_a_quarantined_stock_has_no_event_and_lead_times_count_the_last_5_20_cross():
    data, sessions = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    found, fast_days = ps.crosses(data)
    lag = SLOW_K - max(j for j in fast_days["005930"] if j <= SLOW_K)
    bins = ps.lead_times(found, fast_days)
    assert sum(bins.values()) == 1
    assert [name for name, n in bins.items() if n] == [next(f"{lo}-{hi}" for lo, hi in ps.LEAD_BINS
                                                            if lo <= lag <= hi)]
    events = t._events(("005930", "restructuring", sessions[FAST_K - 5]))
    assert [e for e in ps.crosses(data, events)[0] if e["symbol"] == "005930" and e["k"] == FAST_K] == []


def test_study_reports_a_verdict_only_for_the_two_new_trials_and_runs_once(tmp_path):
    data, _ = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    result = ps.study(data)
    assert set(result["signals"]) == {"gc20_60", "gc5_20", "gc5_20_vol"}
    assert "edge" not in result["signals"]["gc20_60"]
    assert result["signals"]["gc5_20"]["edge"] is False                    # one cohort: no t
    assert result["signals"]["gc5_20"]["development"]["events"] == 1
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        ps.main(["--input", str(tmp_path), "--events", str(tmp_path / "e.json"), "--record", str(record)])
