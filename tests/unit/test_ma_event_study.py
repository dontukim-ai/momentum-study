"""tools/ma_event_study.py (v23) on synthetic inputs, outcomes computed by hand."""

import importlib.util
import math
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from momentum.ledger.ledger import CorporateAction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("ma_event_study", ROOT / "tools/ma_event_study.py")
es = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = es
spec.loader.exec_module(es)

N = 220
PATH = t.path_down_up_down(N)
GOLDEN = t.golden_and_dead_days(PATH, t.weekdays(N))["golden"]


def test_newey_west_t_by_hand():
    # mean 2.5; gamma0 1.25, gamma1 -0.4375 at Bartlett weight 1/2: var 0.8125, se sqrt(0.8125/4)
    assert es.newey_west_t([1, 3, 2, 4], 1) == pytest.approx(2.5 / math.sqrt(0.8125 / 4))
    assert es.newey_west_t([1, 2, 3], 0) == pytest.approx(2 / math.sqrt(2 / 9))
    assert math.isnan(es.newey_west_t([1.0], 3))


def test_one_golden_cross_is_one_event_bought_at_the_next_open():
    data, sessions = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    found = es.events(data)
    assert [(e["symbol"], e["day"], e["signals"]) for e in found] == [("005930", sessions[GOLDEN], ["golden"])]
    rows, left = es.outcomes(data, found, 5)
    expected = PATH[GOLDEN + 5] / PATH[GOLDEN + 1] - 1               # open = close in these bars, 069500 flat
    assert rows[0]["ret"] == pytest.approx(expected) and rows[0]["bench"] == 0
    assert rows[0]["net_excess"] == pytest.approx(expected - 0.0073)
    assert left == {"missing": 0, "unexplained": 0, "open_at_end": 0, "no_benchmark": 0}


def test_a_recorded_split_in_the_window_is_applied_and_an_unrecorded_one_is_left_out():
    sessions = t.weekdays(N)
    at = GOLDEN + 3
    raw = [p / 5 if k >= at else p for k, p in enumerate(PATH)]
    split = SimpleNamespace(action=CorporateAction("005930", "split", sessions[at], None, Decimal(5), None),
                            available_at=datetime.combine(sessions[at - 10], datetime.min.time(), t.KST))
    data, _ = t.make_data({"005930": raw, "069500": [10000] * N}, actions=[split], n=N)
    rows, _ = es.outcomes(data, es.events(data), 5)
    assert rows[0]["ret"] == pytest.approx(PATH[GOLDEN + 5] / PATH[GOLDEN + 1] - 1)
    data.actions = ()
    rows, left = es.outcomes(data, es.events(data), 5)
    assert rows == [] and left["unexplained"] == 1


def test_a_missing_exit_bar_is_left_out_not_filled():
    data, sessions = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    data.snapshot.bars = [b for b in data.snapshot.bars
                          if not (b.symbol == "005930" and b.session == sessions[GOLDEN + 5])]
    rows, left = es.outcomes(data, es.events(data), 5)
    assert rows == [] and left["missing"] == 1


def test_study_reports_both_signals_and_runs_once(tmp_path, monkeypatch):
    data, _ = t.make_data({"005930": PATH, "069500": [10000] * N}, n=N)
    result = es.study(data)
    assert set(result["signals"]) == {"golden_bull", "golden"}
    assert result["signals"]["golden"]["h5"]["development"]["events"] == 1
    assert result["signals"]["golden_bull"]["h5"]["development"] == {"events": 0}
    assert result["signals"]["golden"]["edge"] is False                     # one cohort: no t
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        es.main(["--input", str(tmp_path), "--record", str(record)])


def test_a_golden_cross_in_bull_alignment_counts_for_both_signals():
    path = t.rising_after_dip(300, [200.0])
    k = t.first_golden_bull(path)
    data, sessions = t.make_data({"005930": path, "069500": [10000] * 300}, n=300)
    found = [e for e in es.events(data) if e["day"] == sessions[k]]
    assert [e["signals"] for e in found] == [["golden", "golden_bull"]]
