"""tools/return_lab.py on synthetic minute bars (no network, no real data)."""

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("return_lab", ROOT / "tools/return_lab.py")
lab = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = lab
spec.loader.exec_module(lab)
dl = lab.dl


def rows_from(prices):
    out = []
    for i, p in enumerate(prices):
        m = dl.OPEN + i
        out.append({"stck_cntg_hour": f"{m // 60:02d}{m % 60:02d}00", "stck_prpr": str(p), "cntg_vol": "1000"})
    return out


def synthetic(days, pattern, seed=1):
    """'alternate': each tick mostly reverses the last; 'random': a random walk;
    'regime': a random walk whose step is 5 before 12:00 and 50 after."""
    rng = np.random.default_rng(seed)
    bars = {}
    for symbol in ("069500", "102110", "229200"):
        bars[symbol] = {}
        for d in range(days):
            p, last, prices = 100000, 5, []
            for i in range(dl.GRID):
                if pattern == "alternate" and rng.random() < 0.9:
                    step = -last
                else:
                    size = 50 if pattern == "regime" and dl.OPEN + i >= 12 * 60 else 5
                    step = int(rng.choice([-size, size]))
                p, last = p + step, step
                prices.append(p)
            day = f"2026-01-{d + 1:02d}" if d < 31 else f"2026-02-{d - 30:02d}"
            bars[symbol][day] = rows_from(prices)
    return bars


def test_cell_metrics_by_hand():
    pred = np.array([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 3)
    actual = pred * 2.0
    day = np.zeros(30, dtype=int)
    c = lab.score_cell(pred, actual, day, baseline=np.zeros(30))
    assert c["ic"] == pytest.approx(1.0) and c["days"] == 1 and math.isnan(c["day_ic_t"])
    assert c["decile_spread_bp"] == pytest.approx(20.0 - 2.0)        # top 3 (10s) minus bottom 3 (1s)
    assert c["r2"] == pytest.approx(1 - np.sum((actual - pred) ** 2) / np.sum(actual ** 2))
    flat = lab.score_cell(np.zeros(30), actual, day, baseline=np.zeros(30))
    assert math.isnan(flat["ic"]) and flat["r2"] == 0.0


def test_a_planted_reversal_is_found_as_a_return_forecast():
    data = dl.build(synthetic(45, "alternate"))
    cells = lab.walk_forward(data, initial=40, refit=5)["cells"]
    ridge = cells["RIDGE h1"]
    assert ridge["day_ic"] > 0.5 and ridge["day_ic_t"] > 2 and ridge["r2"] > 0.3 and ridge["bonferroni_significant"]
    assert math.isnan(cells["ZERO h1"]["ic"]) and cells["ZERO h1"]["r2"] == 0.0


def test_a_random_walk_has_no_return_forecast_but_volatility_regimes_have_size():
    data = dl.build(synthetic(58, "regime", seed=3))   # 18 test days: h30 windows overlap heavily
    cells = lab.walk_forward(data, initial=40, refit=5)["cells"]
    assert not any(cells[f"RIDGE h{h}"]["bonferroni_significant"] for h in dl.HORIZONS)
    assert all(abs(cells[f"RIDGE h{h}"]["day_ic"]) < 0.1 for h in dl.HORIZONS)
    size = cells["SIZE h5"]
    assert size["ic"] > 0.4 and size["r2"] > 0.2


def test_frozen_models_round_trip_and_the_holdout_stays_sealed(tmp_path):
    frozen = lab.freeze(dl.build(synthetic(12, "alternate")))
    again = json.loads(json.dumps(frozen))
    cells = lab.score_frozen(again, dl.build(synthetic(3, "alternate", seed=9)))["cells"]
    assert cells["RIDGE h1"]["ic"] > 0.5
    with pytest.raises(SystemExit, match="sealed"):
        dl.forward_bars(tmp_path, "069500", "2026-10-23")
