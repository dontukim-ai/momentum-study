"""tools/pattern_study.py (v28) on synthetic inputs, shapes computed by hand."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("pattern_study", ROOT / "tools/pattern_study.py")
pt = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = pt
spec.loader.exec_module(pt)

N, K = 220, 120


def fall_then_rebound(n=N, k=K, low=92.0, rise=1.11, high=100.0):
    """Flat at ``high`` to k-20, down to ``low`` at k-5, up by ``rise`` to k, flat after."""
    out = []
    for j in range(n):
        if j <= k - 20:
            out.append(high)
        elif j <= k - 5:
            out.append(high + (low - high) * (j - (k - 20)) / 15)
        elif j <= k:
            out.append(low * (1 + (rise - 1) * (j - (k - 5)) / 5))
        else:
            out.append(low * rise)
    return out


def test_shape_is_excess_over_069500_on_each_leg_by_hand():
    stock = [100.0] * 6 + [92.0] * 10 + [92.0, 95.0, 98.0, 100.0, 102.12]       # 21 closes: k-20 .. k
    etf = [10000.0] * 6 + [9900.0] * 15                                          # 069500 -1% on the fall leg
    fall, rebound = pt.shape(stock, etf)
    assert fall == pytest.approx(-0.08 - (-0.01))
    assert rebound == pytest.approx(102.12 / 92 - 1)
    assert pt.hit(stock, etf)
    assert not pt.hit(stock, [10000.0] * 6 + [9300.0] * 15)                       # 069500 fell 7%: no fall in excess


def test_one_event_on_the_first_day_of_the_shape_and_the_path_is_v27s():
    path = fall_then_rebound()
    data, _ = t.make_data({"005930": path, "069500": [10000] * N}, n=N)
    found = pt.events(data)
    assert [(e["symbol"], e["k"]) for e in found] == [("005930", K)]
    rows, left = pt.ps.paths(data, found)
    assert rows[0]["h20"] == pytest.approx(0 - 0.0073)                            # flat after the rebound
    assert left == {"missing": 0, "unexplained": 0, "open_at_end": 0}


def test_a_shape_held_two_days_is_one_event():
    path = fall_then_rebound(low=90.0)
    path[K + 1] = path[K] * 1.03                                                  # the shape still holds on k+1
    data, _ = t.make_data({"005930": path, "069500": [10000] * N}, n=N)
    assert pt.hit(path[K - 20:K + 2], [1.0] * 22) and pt.hit(path[K - 21:K + 1], [1.0] * 22)
    assert [e["k"] for e in pt.events(data)] == [K]


def test_a_quarantined_stock_has_no_event_and_the_study_runs_once(tmp_path):
    data, sessions = t.make_data({"005930": fall_then_rebound(), "069500": [10000] * N}, n=N)
    assert pt.events(data, t._events(("005930", "restructuring", sessions[K - 30]))) == []
    result = pt.study(data)
    entry = result["signals"]["drop_rebound"]
    assert entry["development"]["events"] == 1 and entry["edge"] is False
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        pt.main(["--input", str(tmp_path), "--events", str(tmp_path / "e.json"), "--record", str(record)])
