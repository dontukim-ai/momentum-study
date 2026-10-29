"""tools/pattern_lab.py on synthetic minute bars (no network, no real data)."""

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("pattern_lab", ROOT / "tools/pattern_lab.py")
lab = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = lab
spec.loader.exec_module(lab)
GRID, OPEN = lab.direction_lab.GRID, lab.direction_lab.OPEN


def bar(i, o, h, low, c, v=1000):
    m = OPEN + i
    return {"stck_cntg_hour": f"{m // 60:02d}{m % 60:02d}00", "stck_oprc": str(o), "stck_hgpr": str(h),
            "stck_lwpr": str(low), "stck_prpr": str(c), "cntg_vol": str(v)}


def flat_day(price=10000, skip=()):
    return [bar(i, price, price, price, price) for i in range(GRID) if i not in skip]


def test_a_minute_without_trades_is_a_zero_range_bar_at_the_last_close():
    rows = flat_day(skip={0, 1, 50})
    rows[47] = bar(49, 10000, 10030, 9990, 10020)  # rows index 47 is minute 49 (0 and 1 skipped)
    o, h, low, c, v = lab.ohlcv(rows)
    assert np.isnan(c[:2]).all() and np.isnan(o[:2]).all()
    assert (o[50], h[50], low[50], c[50], v[50]) == (10020, 10020, 10020, 10020, 0)


def test_features_by_hand():
    rows = flat_day()
    rows[100] = bar(100, 10000, 10030, 9980, 10020, v=3000)  # minute 100 = i, the newest of the five
    s = lab.day_samples(*lab.ohlcv(rows))
    k = 100 - lab.START                             # sample k is minute START + k
    newest = s["X"][k][-5:]                         # ret, body, upper, lower, vol of minute i
    assert newest[0] == pytest.approx(20.0)        # 10020 / 10000 - 1 = 20bp
    assert newest[1] == pytest.approx(20.0)        # body (10020 - 10000) / 10000
    assert newest[2] == pytest.approx(10.0)        # upper (10030 - 10020) / 10000
    assert newest[3] == pytest.approx(20.0)        # lower (10000 - 9980) / 10000
    assert newest[4] == pytest.approx(np.log(3001 / 1001))
    assert s["X"].shape[1] == 25 and s["y5"][k] == -1  # back to 10000 one minute later, flat after
    assert s["r5"][k] == pytest.approx(10000 / 10020 - 1)


def test_samples_run_from_0930_to_1514_and_never_read_later_bars():
    rng = np.random.default_rng(1)
    path = 10000 + np.cumsum(rng.choice([-10, 0, 10], GRID))
    rows = [bar(i, p, p + 10, p - 10, p) for i, p in enumerate(path)]
    a = lab.day_samples(*lab.ohlcv(rows))
    assert len(a["X"]) == GRID - 5 - lab.START
    later = [dict(r) for r in rows]
    for r in later[200:]:
        for key in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr"):
            r[key] = str(float(r[key]) + 500)
    b = lab.day_samples(*lab.ohlcv(later))
    upto = 200 - lab.START
    assert np.array_equal(a["X"][:upto], b["X"][:upto])
    assert not np.array_equal(a["y5"][upto - 5:upto], b["y5"][upto - 5:upto])  # targets look ahead, by design


def planted_days(n, seed=0):
    """Each minute reverses the last one 85% of the time, so five minutes on end against the last bar."""
    rng = np.random.default_rng(seed)
    data = {}
    for d in range(n):
        price, path, step = 10000.0, [], 10
        for _ in range(GRID):
            step = -step if rng.random() < 0.85 else step
            price += step
            path.append(price)
        rows = [bar(i, p, p, p, p, v=int(rng.integers(500, 1500))) for i, p in enumerate(path)]
        data[f"2026-01-{d + 1:02d}"] = lab.day_samples(*lab.ohlcv(rows))
    return data


def test_every_model_beats_a_coin_on_a_planted_pattern_except_base():
    data = planted_days(8)
    result = lab.walk_forward(data, initial=4, refit=2, k=50)
    assert set(result["cells"]) == {f"{m} h5" for m in lab.MODELS}
    for m in ("PATTERN", "LOGIT", "KNN"):
        assert result["cells"][f"{m} h5"]["accuracy"] > 0.6, m


def test_knn_probability_is_smoothed_neighbour_share():
    X = np.array([[0.0], [0.1], [0.2], [5.0], [5.1]])
    y = np.array([1, 1, -1, -1, -1])
    params = lab.fit("KNN", X, y, k=3)
    p = lab.predict("KNN", params, np.array([[0.05], [5.05]]), k=3)
    assert p[0] == pytest.approx((2 + 1) / (3 + 2)) and p[1] == pytest.approx((0 + 1) / (3 + 2))


def test_pattern_state_is_five_signs_and_a_volume_flag():
    X = np.zeros((1, 25))
    X[0, 0::5] = [5, -3, 0, 2, -1]
    X[0, 4::5] = [0.5, 0.5, -0.1, 0.2, 0.0]
    assert lab.states(X).tolist() == [[1, -1, 0, 1, -1, 1]]


def test_freeze_round_trips_and_score_refuses_days_before_forward(tmp_path):
    data = planted_days(3)
    frozen = json.loads(json.dumps(lab.freeze(data, k=20)))
    result = lab.score_frozen(frozen, data, k=20)
    assert result["cells"]["PATTERN h5"]["called"] > 0
    with pytest.raises(SystemExit):
        lab.direction_lab.forward_bars(tmp_path, lab.SYMBOL, "2026-10-23")


def test_rich_bar_types_by_hand():
    X = np.zeros((1, 25))
    X[0, 0::5] = [5, -3, 0, 2, -1]                 # returns in bp, baseline 2bp
    X[0, 4::5] = [0.5, 0.5, -0.1, 0.2, 0.0]        # log volume vs baseline
    # big up/high, big down/high, flat/low, small up/high (2 is not above 2), small down/low
    assert lab.bar_types(X, np.array([2.0])).tolist() == [[9, 1, 4, 7, 2]]


def test_magnitude_baseline_is_the_median_of_nonzero_moves_before_the_bars():
    path = [10000] * GRID
    for m, step in ((80, 2), (85, -4), (90, 6)):       # three moves in the window 77..95 of minute 100
        for k in range(m, GRID):
            path[k] += step
    s = lab.day_samples(*lab.ohlcv([bar(i, p, p, p, p) for i, p in enumerate(path)]))
    k = 100 - lab.START
    assert s["mag"][k] == pytest.approx(np.median([2 / 10000, 4 / 10002, 6 / 9998]) * 1e4)


def test_online_record_registers_new_patterns_and_backs_off():
    rec = lab.OnlineRecord(minimum=2)
    a, b = (1, 2, 3, 4, 5), (9, 9, 9, 4, 5)
    assert rec.prob(a) == 0.5                         # never seen: no call
    rec.add(a, 1)
    rec.add(a, 1)
    rec.add(a, -1)
    assert rec.prob(a) == pytest.approx((2 + 1) / (3 + 2))
    assert rec.prob(b) == pytest.approx((2 + 1) / (3 + 2))   # unseen, falls back to the shared last two bars
    rec.add(a, 0)                                     # flat outcomes are not counted
    assert rec.prob(a) == pytest.approx(3 / 5)


def test_online_never_uses_an_outcome_before_it_is_known():
    data = planted_days(6)
    base = lab.online(data, initial=4)
    day = list(data)[4]
    i = 50
    changed = {d: {key: v.copy() for key, v in s.items()} for d, s in data.items()}
    changed[day]["y5"][i] = -changed[day]["y5"][i] if changed[day]["y5"][i] else 1
    other = lab.online(changed, initial=4)
    n = len(data[day]["y5"])
    # sample i's outcome is known only at minute i + 5 (sample i + 5): every earlier prediction is unchanged
    assert np.array_equal(base["p"][:i + 5], other["p"][:i + 5])
    assert base["p"].shape[0] == n + len(data[list(data)[5]]["y5"])


def test_online_learns_a_planted_pattern():
    result = lab.online(planted_days(8), initial=4)
    assert result["cell"]["accuracy"] > 0.6
