"""tools/learning_ledger.py: the v13 trial ledger, stage ladder and learning curve."""

import copy
import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("learning_ledger", ROOT / "tools/learning_ledger.py")
ledger_tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ledger_tool
spec.loader.exec_module(ledger_tool)
L = ledger_tool


def trial(name, kind="strategy", t=3.5, mean=0.001):
    return {"id": f"x/{name}", "study": "x", "family": "F", "name": name, "kind": kind,
            "registered": "2026-10-27", "development": ["2025-10-27", "2026-06-30"],
            "dev": {"t": t, "net_return": 0.1, "mean_daily": mean}, "cost_version": "v11-1tick"}


def test_threshold_rises_with_the_number_of_trials():
    assert L.threshold(1) == L.threshold(2) == 2.0            # 1.64 and 1.96 are floored at 2
    assert L.threshold(20) == pytest.approx(2.81, abs=0.01)
    assert L.threshold(54) == pytest.approx(3.11, abs=0.01)
    assert L.threshold(1000) == pytest.approx(3.89, abs=0.01)


def test_register_appends_s0_and_refuses_duplicates():
    book = L.empty()
    L.register(book, [trial("a"), trial("b")])
    assert [t["stage"] for t in book["trials"]] == ["S0", "S0"]
    assert book["trials"][0]["history"] == [{"stage": "S0", "date": "2026-10-27", "reason": "registered"}]
    with pytest.raises(SpecUndefinedError, match="already"):
        L.register(book, [trial("a")])


def test_ladder_moves_one_step_and_caps_strategies():
    book = L.empty()
    L.register(book, [trial(str(k)) for k in range(7)] + [trial("f", kind="forecast")])
    for k in range(5):
        L.advance(book, f"x/{k}", "S1", "2026-11-30", "dev passed")
    with pytest.raises(SpecUndefinedError, match="S1 is full"):
        L.advance(book, "x/5", "S1", "2026-11-30", "dev passed")
    L.advance(book, "x/f", "S1", "2026-11-30", "frozen")          # forecasts do not take strategy seats
    with pytest.raises(SpecUndefinedError, match="S0 -> S2"):
        L.advance(book, "x/6", "S2", "2026-11-30", "skip")
    L.advance(book, "x/0", "closed", "2026-12-31", "shadow failed")
    with pytest.raises(SpecUndefinedError, match="closed"):
        L.advance(book, "x/0", "S1", "2027-01-31", "again")


def test_dev_gate_uses_the_threshold_of_the_whole_ledger():
    book = L.empty()
    L.register(book, [trial(str(k), t=3.0) for k in range(60)])
    with pytest.raises(SpecUndefinedError, match="below the ledger threshold"):
        L.advance(book, "x/0", "S1", "2026-11-30", "dev passed")
    assert book["trials"][0]["stage"] == "S0"


def test_frozen_fields_cannot_change():
    book = L.empty()
    L.register(book, [trial("a")])
    later = copy.deepcopy(book)
    later["trials"][0]["dev"]["t"] = 9.0
    with pytest.raises(SpecUndefinedError, match="x/a"):
        L.check_append_only(book, later)
    dropped = copy.deepcopy(book)
    dropped["trials"] = []
    with pytest.raises(SpecUndefinedError, match="removed"):
        L.check_append_only(book, dropped)
    pending = L.empty()
    L.register(pending, [{**trial("p"), "dev": {"t": None, "mean_daily": None}}])
    filled = copy.deepcopy(pending)
    filled["trials"][0]["dev"]["t"] = 2.4                             # a pending result arrives once
    L.check_append_only(pending, filled)
    refilled = copy.deepcopy(filled)
    refilled["trials"][0]["dev"]["t"] = 3.0
    with pytest.raises(SpecUndefinedError, match="x/p"):
        L.check_append_only(filled, refilled)
    grown = copy.deepcopy(book)
    L.advance(grown, "x/a", "S1", "2026-11-30", "dev passed")
    L.check_append_only(book, grown)                                 # stage moves are allowed


def test_cycle_records_shrinkage_and_the_promotion_hit_rate():
    book = L.empty()
    L.register(book, [trial("a", mean=0.002), trial("b", mean=0.001), trial("c", mean=None)])
    for name in "abc":
        L.advance(book, f"x/{name}", "S1", "2026-11-30", "dev passed")
    row = L.cycle(book, "2026-12", {"x/a": {"mean_daily": 0.001, "t": 1.2, "days": 21},
                                    "x/b": {"mean_daily": -0.0005, "t": -0.4, "days": 21},
                                    "x/c": {"mean_daily": 0.0, "t": 0.0, "days": 21}})
    assert row["candidates"] == 3 and row["oos_mean_daily_bp"] == pytest.approx((10 - 5 + 0) / 3)
    assert row["shrinkage_median"] == pytest.approx(0.0)  # a: 0.5, b: -0.5, c: no dev mean
    assert row["promotion_hit_rate"] is None                            # nobody has finished S1 yet
    L.advance(book, "x/a", "S2", "2027-02-28", "shadow passed")
    L.advance(book, "x/b", "closed", "2027-02-28", "shadow failed")
    # Left S1: a up to S2, b closed -> 1 of 2.
    assert L.cycle(book, "2027-02", {"x/a": {"mean_daily": 0.001, "t": 1.0, "days": 21}})["promotion_hit_rate"] == 0.5
    with pytest.raises(SpecUndefinedError, match="already recorded"):
        L.cycle(book, "2026-12", {})
    with pytest.raises(SpecUndefinedError, match="not in S1/S2"):
        L.cycle(book, "2027-03", {"x/b": {"mean_daily": 0.0, "t": 0.0, "days": 21}})


def test_the_committed_ledger_is_valid_and_counts_every_trial():
    book = json.loads((ROOT / "config/learning-v13/ledger.json").read_text(encoding="utf-8"))
    L.validate(book)
    studies = {t["study"] for t in book["trials"]}
    assert {"v11", "v12", "v12b"} <= studies
    assert sum(t["study"] == "v11" for t in book["trials"]) == 27
    assert math.isclose(L.threshold(len(book["trials"])), L.threshold(54))
