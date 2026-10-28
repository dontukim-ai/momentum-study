"""tools/hypotheses_v15.py on synthetic data (no network)."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("hypotheses_v15", ROOT / "tools/hypotheses_v15.py")
hv = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = hv
spec.loader.exec_module(hv)
E = hv.EtfDay


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def test_statistics_by_hand():
    assert hv.t_mean([1.0, 2.0, 3.0]) == pytest.approx(2.0 / (1.0 / 3 ** 0.5))
    beta, t = hv.ols([0.0, 1.0, 2.0, 3.0], [0.0, 2.0, 4.1, 5.9])
    assert beta == pytest.approx(1.98) and t > 10                   # Sxy 9.9 / Sxx 5
    x = np.array([1.0, -1.0, 1.0, -1.0])
    b, _ = hv.clustered_slope(x, -0.5 * x, np.array([0, 0, 1, 1]))
    assert b == pytest.approx(-0.5)


def etf_market(n=140):
    """30 domestic ETFs: the first 3 keep taking money and then fall; 069500 rises after inverse inflows."""
    days = weekdays(date(2020, 1, 2), n)
    data = {}
    for k, d in enumerate(days):
        rows = {}
        for i in range(30):
            code = f"{i:06d}"
            shares = 1e6 * (1 + 0.01 * k) if i < 3 else 1e6
            close = 10000 * (1 - 0.002 * k) if i < 3 else 10000 * (1 + 0.0005 * (k % 7))
            rows[code] = E(close, close, shares, 2e10, f"KODEX 테마{i}", "테마지수")
        inv_shares = 1e6 * (1 + (0.02 if (k // 21) % 2 else 0.0) * (k % 21))
        rows["114800"] = E(5000.0, 5000.0, inv_shares, 2e10, "KODEX 인버스", "코스피 200 선물지수")
        rows["069500"] = E(30000.0 * (1 + 0.001 * k), 30000.0, 1e6, 2e10, "KODEX 200", "코스피 200")
        data[d] = rows
    return data


def test_h1_inflow_leaders_lag_and_entries_wait_a_session():
    data = etf_market()
    out = hv.h1(data, "2020-01", "2020-12")
    assert out["H1a"]["months"] >= 3 and out["H1a"]["mean_spread"] < 0
    assert out["H1b"]["months"] >= out["H1a"]["months"]             # H1a also needs 60 listed sessions
    # A different close on a month-end day itself must not change the result (entry is the next session).
    sessions = sorted(data)
    k = hv.month_ends(sessions, "2020-01", "2020-12")[0]
    shifted = {d: dict(r) for d, r in data.items()}
    shifted[sessions[k]]["000000"] = shifted[sessions[k]]["000000"]._replace(close=1.0)
    assert hv.h1(shifted, "2020-01", "2020-12")["H1a"]["mean_spread"] == pytest.approx(out["H1a"]["mean_spread"])


def bar(day, hhmmss, open_, close):
    return {"stck_bsop_date": day.replace("-", ""), "stck_cntg_hour": hhmmss, "stck_oprc": str(open_),
            "stck_prpr": str(close), "cntg_vol": "10"}


def write_auction_days(root, symbol, days, auction_moves, rng):
    (root / symbol).mkdir(parents=True)
    price = 10000.0
    for d, move in zip(days, auction_moves, strict=True):
        rows = [bar(d, "090000", price, price)]
        rows.append(bar(d, "151900", price, price))
        close = price * (1 + move)
        rows.append(bar(d, "153000", close, close))
        (root / symbol / f"{d}.json").write_text(json.dumps({"bars": rows}))
        price = close * (1 - 0.5 * move + rng.normal(0, 1e-4))   # half the auction move comes back at the open


def test_h2_finds_a_planted_overnight_reversal(tmp_path):
    days = weekdays(date(2025, 11, 3), 40)
    rng = np.random.default_rng(0)
    for s in ("A", "B", "C"):
        write_auction_days(tmp_path, s, days, list(rng.normal(0, 0.003, len(days))), rng)
    out = hv.h2(tmp_path, ["A", "B", "C"], "2025-11-01", "2026-06-30")
    assert out["slope"] == pytest.approx(-0.5, abs=0.05) and out["t"] < -5
    assert out["pairs"] == 3 * 39 and out["aux_trades"] > 0 and out["aux_mean_after_cost"] > 0


def test_auction_points_use_the_right_bars():
    rows = [bar("2025-11-03", "090000", 100, 101), bar("2025-11-03", "151900", 102, 103),
            bar("2025-11-03", "152500", 0, 0), bar("2025-11-03", "153000", 104, 105)]
    rows = [r for r in rows if r["stck_cntg_hour"] != "152500"]
    assert hv.auction_points(rows) == (103.0, 105.0, 100.0)


def test_h3_sibling_deviation_reverts(tmp_path):
    rng = np.random.default_rng(1)
    days = weekdays(date(2025, 11, 3), 5)
    for s in hv.SIBLINGS:
        (tmp_path / s).mkdir(parents=True)
    for d in days:
        common = 10000 * np.exp(np.cumsum(rng.normal(0, 2e-4, hv.dl.GRID)))
        for j, s in enumerate(hv.SIBLINGS):
            noise = rng.normal(0, 3e-4, hv.dl.GRID)            # independent, short-lived
            prices = np.round(common * (1 + 0.1 * j) * np.exp(noise))
            rows = [bar(d, f"{(hv.dl.OPEN + m) // 60:02d}{(hv.dl.OPEN + m) % 60:02d}00", p, p)
                    for m, p in enumerate(prices)]
            (tmp_path / s / f"{d}.json").write_text(json.dumps({"bars": rows}))
    out = hv.h3(tmp_path, "2025-11-01", "2026-06-30")
    assert out["slope"] < -0.5 and out["t"] < -5 and out["points"] > 0


def test_validation_needs_dev_and_runs_once(tmp_path, monkeypatch):
    with pytest.raises(SpecUndefinedError, match="development record"):
        hv.run("validation", tmp_path, tmp_path, [], tmp_path / "rec")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "dev.json").write_text(json.dumps({"passed_dev": []}))
    out = hv.run("validation", tmp_path, tmp_path, [], rec)
    assert out["ran"] == [] and out["passed_validation"] == {}
    with pytest.raises(SpecUndefinedError, match="already ran"):
        hv.run("validation", tmp_path, tmp_path, [], rec)
    with pytest.raises(SpecUndefinedError, match="recorded once"):
        hv.run("dev", tmp_path, tmp_path, [], rec)


def test_dev_pass_follows_the_registered_directions():
    rec = {"H1": {"H1a": {"t": -2.5}, "H1b": {"t": 1.9}}, "H2": {"t": float("nan")}}
    assert hv.dev_passed(rec) == ["H1a"]
    rec["H1"]["H1a"]["t"] = 2.5                                      # significant the other way is not a pass
    assert hv.dev_passed(rec) == []
