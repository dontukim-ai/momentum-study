"""tools/hypotheses_v16.py on synthetic data (no network)."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hypotheses_v16", ROOT / "tools/hypotheses_v16.py")
hv = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = hv
spec.loader.exec_module(hv)
S = hv.Stock


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_corporate_action_days_are_quarantined_not_repaired():
    days = weekdays(date(2024, 1, 2), 3)
    data = {days[0]: {"000010": S(1000, 1000, 1, 10, 1e11, "KOSPI")},
            days[1]: {"000010": S(1100, 1000, 1, 10, 1e11, "KOSPI")},          # +10%
            days[2]: {"000010": S(220, 200, 1, 50, 1e11, "KOSPI")}}             # 1:5 split, base 200 != 1100
    _, rets, quarantined = hv.daily_returns(data)
    assert rets["000010"] == {1: pytest.approx(0.1)} and quarantined == 1


def test_common_shares_only():
    assert hv.common_share("005930", "삼성전자", "KOSPI")
    assert not hv.common_share("005935", "삼성전자우", "KOSPI")
    assert not hv.common_share("123450", "OO스팩1호", "KOSDAQ") and not hv.common_share("123450", "X", "KONEX")


def test_min_variance_by_hand_and_caps():
    w = hv.min_variance(np.diag([1.0, 4.0]), cap=1.0)
    assert w == pytest.approx([0.8, 0.2], abs=1e-4)
    w = hv.min_variance(np.diag([1.0] + [100.0] * 19), cap=0.1)
    assert w.max() <= 0.1 + 1e-9 and w.sum() == pytest.approx(1.0)


def test_shrinkage_beats_sample_out_of_sample_when_n_is_large():
    rng = np.random.default_rng(3)
    p, t = 60, 80
    true = 0.3 * np.ones((p, p)) + 0.7 * np.eye(p)
    chol = np.linalg.cholesky(true)
    worse = 0
    for _ in range(10):
        x = rng.standard_normal((t, p)) @ chol.T
        shrunk, intensity = hv.ledoit_wolf(x)
        assert 0 < intensity < 1
        ws, wl = hv.min_variance(np.cov(x, rowvar=False, bias=True)), hv.min_variance(shrunk)
        worse += float(ws @ true @ ws) > float(wl @ true @ wl)
    assert worse >= 8                                        # true variance of the shrunk portfolio is lower


def month_data(n_days=70):
    """90 stocks over ~3 months; in Feb, 'L1' stocks (high turnover) spike +25% once and then fall 1% a day in March."""
    days = weekdays(date(2024, 1, 2), n_days)
    rng = np.random.default_rng(5)
    groups = ["L1"] * 30 + ["L2"] * 30 + ["Q"] * 30
    vol = [rng.uniform(5e5, 1e6) if g != "Q" else 1e4 for g in groups]
    price = [10000.0] * 90
    data = {}
    for k, d in enumerate(days):
        rows = {}
        for i, g in enumerate(groups):
            r = rng.normal(0, 0.002)
            if g == "L1" and d == "2024-02-15":
                r = 0.25
            if g == "L1" and d >= "2024-03-04":
                r = -0.01
            prev = price[i]
            price[i] = round(prev * (1 + r))
            rows[f"{i:05d}0"] = S(price[i], prev if k else price[i], vol[i], 1e6, 1e11, "KOSDAQ")
        data[d] = rows
    return data


def test_d1_planted_lottery_effect_in_the_high_turnover_tercile():
    data = month_data()
    sessions, rets, _ = hv.daily_returns(data)
    out = hv.d1(data, sessions, rets, "2024-02", "2024-02")
    assert out["high_turnover"]["months"] == 1 and out["high_turnover"]["mean"] < -0.05
    assert out["low_turnover"]["months"] == 1 and abs(out["low_turnover"]["mean"]) < 0.02


def test_month_bounds_need_a_full_next_month():
    days = weekdays(date(2024, 1, 2), 70)                     # Jan..early Apr
    bounds = hv.month_bounds(days, "2024-01", "2024-12")
    assert [days[end][:7] for _, end, _ in bounds] == ["2024-02"]


def test_validation_needs_dev_and_runs_once(tmp_path):
    with pytest.raises(SpecUndefinedError, match="development record"):
        hv.run("validation", tmp_path, tmp_path / "rec")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "validation.json").write_text("{}")
    (rec / "dev.json").write_text(json.dumps({"passed_dev": []}))
    with pytest.raises(SpecUndefinedError, match="already ran"):
        hv.run("validation", tmp_path, rec)
    with pytest.raises(SpecUndefinedError, match="recorded once"):
        hv.run("dev", tmp_path, rec)


def test_d3_multiplier_recovers_a_planted_flow_impact():
    rng = np.random.default_rng(7)
    days = weekdays(date(2024, 1, 2), 120)
    E = hv.Etf
    etfs, stocks, shares, inverse, level = {}, {}, 1e7, 1e7, 300.0
    for d in days:
        flow = rng.normal(0, 1e9)                               # won into KOSPI200 trackers
        back = rng.normal(0, 5e8)                               # won into the inverse ETF (exposure -1)
        level *= 1 + 5 * (flow - back) / 2e15 + rng.normal(0, 1e-7)   # M = 5 on a 2,000조 market, small noise
        shares += flow / 10000
        inverse += back / 5000
        etfs[d] = {"069500": E(10000.0, shares, level), "114800": E(5000.0, inverse, 999.0),
                   "999990": E(8000.0, 1e6, 1.0)}                # unrelated index: ignored
        stocks[d] = {"005930": S(1, 1, 1, 1, 2e15, "KOSPI")}
    out = hv.d3(etfs, stocks, "2024-01", "2024-12")
    lag0 = out["by_lag"]["0"]
    assert lag0["multiplier"] == pytest.approx(5.0, rel=0.1) and lag0["t"] > 10
    assert abs(out["by_lag"]["1"]["t"]) < 3
