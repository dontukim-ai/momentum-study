"""tools/ma_grid.py with stand-in backtests (no real data)."""

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ma_grid", ROOT / "tools/ma_grid.py")
grid = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = grid
spec.loader.exec_module(grid)


def weekdays(start, end):
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def fake_run(edge_of, seen):
    """A stand-in backtest: the strategy beats a flat benchmark by ``edge_of(rule)`` a day, with a wobble."""
    def run(data, bps, rule, last_day=None, first_trade=grid.ma.FIRST_TRADE, **_):
        seen.append((rule.name, last_day))
        if edge_of(rule) is None:
            raise grid.ma.SpecUndefinedError("held 221610 moved x45")
        days = weekdays(first_trade, last_day or date(2026, 10, 20))
        nav, bench = [1.0], [1.0]
        for k in range(1, len(days)):
            nav.append(nav[-1] * (1 + edge_of(rule) + (0.001 if k % 2 else -0.001)))
            bench.append(1.0)
        return {"days": days, "nav": nav, "bench": bench, "fills": [], "fractions_dropped": 0,
                "counts": {"stop_exits": 0, "take_exits": 0}}
    return run


def test_select_reads_development_only_and_keeps_the_highest_t_skipping_halted_cells():
    seen = []

    def edge(rule):
        if (rule.stop, rule.take) == (0.10, 0.30):
            return None                               # halted by the safety stop
        return 0.0001 * (1 + (rule.stop == 0.07) + (rule.take == 0.20))

    out = grid.select(None, fake_run(edge, seen))
    assert len(out["grid"]) == 20 and {last for _, last in seen} == {grid.DEV[1]}
    assert (out["stop"], out["take"]) == (0.07, 0.20)
    assert sum("stopped" in r for r in out["grid"]) == 1


def test_validation_runs_only_the_chosen_cell_over_the_whole_history():
    seen = []
    rep = grid.validate(None, 0.07, 0.20, fake_run(lambda r: 0.0002, seen))
    assert {name for name, _ in seen} == {"v22 s7 t20"} and {last for _, last in seen} == {None}
    assert set(rep) == set(grid.ma.COSTS) and {"first_half", "second_half"} <= set(rep["base"]["periods"])


def test_forward_starts_at_the_frozen_date():
    seen = []
    frozen = {"cell": "v22 s7 t20", "stop": 0.07, "take": 0.20}

    def run(data, bps, rule, first_trade, **_):
        seen.append(first_trade)
        days = weekdays(first_trade, first_trade + timedelta(days=30))
        return {"days": days, "nav": [1.0 + 0.001 * k for k in range(len(days))], "bench": [1.0] * len(days),
                "fills": [], "counts": {}}

    out = grid.forward(None, frozen, run=run)
    assert seen == [grid.FORWARD_FROM] * 2 and out["base"]["days"][0] == "2026-10-21"


def test_every_halted_grid_is_refused():
    with pytest.raises(grid.ma.SpecUndefinedError):
        grid.select(None, fake_run(lambda r: None, []))
