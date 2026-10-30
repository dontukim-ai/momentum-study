"""tools/ma_cross_diag.py on synthetic traces (no real data)."""

import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from momentum.ledger.ledger import CorporateAction

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ma_cross_diag", ROOT / "tools/ma_cross_diag.py")
diag = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = diag
spec.loader.exec_module(diag)


def test_an_unrecorded_halving_is_flagged_and_a_recorded_split_is_not():
    d = [date(2021, 11, k) for k in (1, 2, 3, 4)]
    trace = [
        (d[0], 1000.0, {"A": (10, 50.0, 50.0, 50.0), "B": (10, 50.0, 50.0, 50.0)}),
        (d[1], 1000.0, {"A": (10, 50.0, 50.0, 50.0), "B": (10, 50.0, 50.0, 50.0)}),
        (d[2], 750.0, {"A": (10, 25.0, 25.0, 25.0), "B": (50, 10.0, 10.0, 10.0)}),   # A halves; B splits 1:5
        (d[3], 760.0, {"A": (10, 26.0, 26.0, 26.0), "B": (50, 10.0, 10.0, 10.0)}),
    ]
    split = SimpleNamespace(action=CorporateAction("B", "split", d[2], None, Decimal(5), None))
    result = diag.diagnose(trace, [split])
    assert [(j["symbol"], j["day"]) for j in result["jumps"]] == [("A", "2021-11-03")]
    assert result["jumps"][0]["nav_change"] == -250.0
    top = result["largest_moves"][0]
    assert top["day"] == "2021-11-03" and top["top"][0]["symbol"] == "A"
    assert result["jump_days"] == 1 and result["vol_without_jump_days"] < result["vol"]
    assert "unexplained jumps" in diag.text(result)


def test_a_merger_across_a_suspension_sold_at_the_reopening_is_found():
    d = [date(2021, 5, k) for k in (18, 19, 20, 21)]
    trace = [
        (d[0], 1000.0, {"M": (100, 3.0, 3.0, 3.0)}),
        (d[1], 1000.0, {"M": (100, 3.0, None, None)}),          # suspended: no bar
        (d[2], 1000.0, {"M": (100, 3.0, None, None)}),
        (d[3], 2400.0, {"M": (0, 0.0, 140.0, 140.0)}),          # reopens at x46.7 and is sold at that open
    ]
    result = diag.diagnose(trace, [])
    assert [(j["symbol"], j["since"], j["day"], j["sold"]) for j in result["jumps"]] == [
        ("M", "2021-05-18", "2021-05-21", True)]
    assert result["jumps"][0]["nav_change"] == 100 * (140.0 - 3.0)
    assert result["largest_moves"][0]["top"][0]["symbol"] == "M"


def test_a_limit_up_day_is_not_a_jump():
    d = [date(2021, 6, 1), date(2021, 6, 2)]
    trace = [(d[0], 1000.0, {"A": (10, 100.0, 100.0, 100.0)}),
             (d[1], 1003.0, {"A": (10, 130.3, 130.3, 101.0)})]   # limit up a hair past 30% on the close
    assert diag.diagnose(trace, [])["jumps"] == []


def test_tracing_does_not_change_the_backtest():
    sys.path.insert(0, str(ROOT / "tests/unit"))
    import test_ma_cross as t

    plain = diag.ma.backtest(t._random_market(3), 36.5)     # fresh markets: their states draw random flags
    trace: list = []
    traced = diag.ma.backtest(t._random_market(3), 36.5, trace)
    assert plain["nav"] == traced["nav"] and len(trace) == len(traced["days"])
