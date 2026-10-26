"""tools/direction_lab.py on synthetic minute bars (no network, no real data)."""

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("direction_lab", ROOT / "tools/direction_lab.py")
lab = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = lab
spec.loader.exec_module(lab)


def rows_from(prices, volumes=None, skip=()):
    out = []
    for i, p in enumerate(prices):
        if i in skip:
            continue
        m = lab.OPEN + i
        out.append({"stck_cntg_hour": f"{m // 60:02d}{m % 60:02d}00", "stck_prpr": str(p),
                    "cntg_vol": str((volumes or [1000] * len(prices))[i])})
    return out


def test_grid_keeps_the_last_trade_and_nothing_before_the_first():
    prices = [100 + i for i in range(lab.GRID)]
    price, volume = lab.grid(rows_from(prices, skip={0, 1, 10}))
    assert np.isnan(price[:2]).all() and price[2] == 102
    assert price[10] == price[9] == 109 and volume[10] == 0


def test_features_never_use_later_prices():
    rng = np.random.default_rng(0)
    base = 10000 + np.cumsum(rng.choice([-5, 0, 5], lab.GRID))
    a = lab.day_samples(*lab.grid(rows_from(base)), None)
    later = base.copy()
    later[200:] += 500                           # change only minutes >= 200
    b = lab.day_samples(*lab.grid(rows_from(later)), None)
    upto = 200 - lab.WARM                          # sample k is minute WARM + k
    assert np.array_equal(a["X"][:upto], b["X"][:upto])
    assert not np.array_equal(a["y30"][:upto], b["y30"][:upto])  # targets do look ahead, by design


def test_targets_are_signs_with_flats():
    prices = [100] * 100 + [101] * (lab.GRID - 100)
    s = lab.day_samples(*lab.grid(rows_from(prices)), None)
    k = 99 - lab.WARM                                # minute 99 -> 100 is the only move
    assert s["y1"][k] == 1 and s["y1"][k + 1] == 0 and s["y1"][k - 1] == 0
    assert np.isnan(s["y30"][-1])                    # beyond 15:19 is not a target


def synthetic(days, pattern, seed=1):
    """``pattern``: 'alternate' (each tick reverses the last one) or 'random'."""
    rng = np.random.default_rng(seed)
    bars = {}
    for symbol in ("069500", "102110"):
        bars[symbol] = {}
        for d in range(days):
            p, last, prices = 10000, 5, []
            for _ in range(lab.GRID):
                step = -last if pattern == "alternate" and rng.random() < 0.9 else int(rng.choice([-5, 5]))
                p, last = p + step, step
                prices.append(p)
            bars[symbol][f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}"] = rows_from(prices)
    return bars


def test_a_planted_reversal_is_found_and_the_base_rate_is_not():
    data = lab.build(synthetic(45, "alternate"))
    result = lab.walk_forward(data, initial=40, refit=5)
    cells = result["cells"]
    assert cells["MARKOV1 h1"]["accuracy"] > 0.85 and cells["MARKOV1 h1"]["bonferroni_significant"]
    assert cells["LOGIT h1"]["accuracy"] > 0.85
    assert abs(cells["BASE h1"]["accuracy"] - 0.5) < 0.05


def test_a_random_walk_shows_no_edge():
    data = lab.build(synthetic(45, "random", seed=7))
    cells = lab.walk_forward(data, initial=40, refit=5)["cells"]
    assert not any(c["bonferroni_significant"] for c in cells.values())
    assert all(abs(c["accuracy"] - 0.5) < 0.03 for name, c in cells.items() if name.endswith("h1"))


def test_score_abstains_at_one_half_and_clusters_by_day():
    p = np.array([0.6, 0.6, 0.5, 0.4, 0.6, 0.4])
    y = np.array([1, -1, 1, -1, 0, 1])
    day = np.array([0, 0, 0, 1, 1, 1])
    s = lab.score(p, y, day)
    assert (s["moving"], s["called"], s["accuracy"]) == (5, 4, 0.5)
    assert math.isclose(s["flat_share"], 1 / 6)
    lo, hi = s["ci95"]
    assert lo < 0.5 < hi and s["days"] == 2


def test_frozen_models_round_trip_and_score_new_days(tmp_path):
    train = lab.build(synthetic(12, "alternate"))
    frozen = lab.freeze(train)
    again = lab.json.loads(lab.json.dumps(frozen))
    later = lab.build(synthetic(3, "alternate", seed=9))
    cells = lab.score_frozen(again, later)["cells"]
    assert cells["MARKOV1 h1"]["accuracy"] > 0.85 and abs(cells["BASE h1"]["accuracy"] - 0.5) < 0.1


def test_forward_scoring_keeps_the_holdout_sealed(tmp_path):
    import pytest
    with pytest.raises(SystemExit, match="sealed"):
        lab.forward_bars(tmp_path, "069500", "2026-10-23")
