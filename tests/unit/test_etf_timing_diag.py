"""tools/etf_timing_diag.py on synthetic sessions."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("etf_timing_diag", ROOT / "tools/etf_timing_diag.py")
dg = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = dg
spec.loader.exec_module(dg)


def test_drawdown_by_hand():
    days = t.weekdays(5)
    dd = dg.drawdown(days, [100.0, 120.0, 90.0, 110.0, 60.0])
    assert dd["mdd"] == 0.5 and dd["peak_day"] == days[1].isoformat() and dd["trough_day"] == days[4].isoformat()


def test_trace_leaves_the_run_unchanged_and_whipsaws_show_as_round_trips():
    path = t.path_down_up_down(220)
    data, sessions = t.make_data({"069500": path}, n=220)
    plain = dg.et.backtest(data, "ma60", 5.0)
    trace: list = []
    traced = dg.et.backtest(data, "ma60", 5.0, trace)
    assert plain["nav"] == traced["nav"] and len(trace) == len(traced["days"])
    result = dg.diagnose(trace, traced["fills"], sessions[0], sessions[-1])
    trips = result["round_trips"]
    assert trips and all(abs(x["ret"] - (x["sell"] / x["buy"] - 1)) < 1e-12 for x in trips)
    assert result["odd_sessions"] == []                  # held all day: moves with the benchmark
