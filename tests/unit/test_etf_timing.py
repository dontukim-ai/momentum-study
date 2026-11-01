"""tools/etf_timing.py (v24) on synthetic 069500 sessions, fills checked by hand."""

import importlib.util
import json
import sys
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("etf_timing", ROOT / "tools/etf_timing.py")
et = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = et
spec.loader.exec_module(et)


def test_hold_rules_by_hand():
    up = [float(k) for k in range(1, 121)]
    assert et.hold(up[:59], "ma60") is None and et.hold(up, "ma60") is True and et.hold(up, "ma120") is True
    assert et.hold(up[:-1] + [10.0], "ma60") is False            # last close 10 < mean of the last 60
    assert et.hold(up, "ma20_60") is True and et.hold(list(reversed(up)), "ma20_60") is False
    assert et.hold(up[:119], "ma120") is None


def test_switches_trade_at_the_next_open_sized_on_the_deciding_close():
    path = t.path_down_up_down(220)
    data, sessions = t.make_data({"069500": path}, n=220)
    result = et.backtest(data, "ma60", 5.0)
    fills = [f for f in result["fills"] if f.shares > 0]
    states = {k: et.hold(path[:k + 1], "ma60") for k in range(len(path))}
    first_in = next(k for k in range(len(path)) if sessions[k + 1] >= t.ma.FIRST_TRADE and states[k])
    first_out = next(k for k in range(first_in + 1, len(path)) if states[k] is False)
    assert fills[0].side == "buy" and fills[0].session_date == sessions[first_in + 1]
    expected = int((Decimal(100_000_000) * Decimal("0.95") / Decimal(str(path[first_in]))).to_integral_value(ROUND_FLOOR))
    assert fills[0].shares == expected
    assert fills[1].side == "sell" and fills[1].session_date == sessions[first_out + 1]
    assert fills[1].shares == fills[0].shares
    assert result["switches"] >= 2


def test_always_above_its_average_the_variant_is_nearly_the_benchmark():
    path = [100.0 + k for k in range(220)]
    data, _ = t.make_data({"069500": path}, n=220)
    rep = et.report(et.backtest(data, "ma60", 5.0))
    assert rep["switches"] == 1 and rep["time_in_market"] > 0.99
    full = rep["periods"]["full"]
    assert full["strategy_cagr"] == pytest.approx(full["069500_cagr"], abs=0.002)


def test_unknown_variant_short_history_and_second_run_are_refused(tmp_path):
    data, _ = t.make_data({"069500": [100.0] * 220}, n=220)
    with pytest.raises(et.SpecUndefinedError, match="needs 120 closes"):
        et.backtest(data, "ma120", 5.0)                   # 2020-01-01 start: no MA120 by 2020-04-01
    with pytest.raises(et.SpecUndefinedError):
        et.backtest(data, "ma200", 5.0)
    record = tmp_path / "run.json"
    record.write_text(json.dumps({}))
    with pytest.raises(et.SpecUndefinedError):
        et.main(["--input", str(tmp_path), "--record", str(record)])
