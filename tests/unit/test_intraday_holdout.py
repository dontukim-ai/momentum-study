"""tools/intraday_holdout.py: freeze, the manifest gate, one holdout run, the §4 judgement."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("intraday_holdout", ROOT / "tools/intraday_holdout.py")
holdout = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = holdout
spec.loader.exec_module(holdout)
cand = holdout.candidates


def kis_day(day, start=10000, step=0):
    rows, price = [], start
    for m in range(cand.minutes("090000"), cand.minutes("153000") + 1):
        price += step
        rows.append({"stck_bsop_date": day.replace("-", ""), "stck_cntg_hour": cand.hhmm(m), "stck_oprc": str(price),
                     "stck_hgpr": str(price), "stck_lwpr": str(price), "stck_prpr": str(price), "cntg_vol": "100000"})
    return rows


def write_bars(root, symbols, days):
    for symbol in symbols:
        (root / symbol).mkdir(parents=True, exist_ok=True)
        for k, day in enumerate(days):
            (root / symbol / f"{day}.json").write_text(json.dumps({"bars": kis_day(day, 10000 + 10 * k)}))


def weekdays(start, count):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


UNIVERSE = ("069500", "114800", "229200", "251340", "102110", "278530")


def test_freeze_writes_names_and_code_hashes_once(tmp_path):
    path = tmp_path / "manifest.json"
    manifest = holdout.freeze(["C5-A 069500"], UNIVERSE, path, frozen_at="2026-10-27T16:00:00+09:00")
    saved = json.loads(path.read_text())
    assert saved == manifest and saved["selected"] == ["C5-A 069500"]
    assert set(saved["code"]) == {"tools/intraday_engine.py", "tools/intraday_candidates.py", "tools/intraday_holdout.py"}
    assert saved["holdout"] == ["2026-07-01", "2026-10-23"] and saved["criteria"]["t"] == 1.5
    with pytest.raises(SpecUndefinedError, match="exists"):
        holdout.freeze(["C5-A 069500"], UNIVERSE, path)


@pytest.mark.parametrize("names", [[], ["no such config"], ["C5-A 069500", "C5-A 069500"],
                                   ["C1 kospi 1450 all", "C1 kospi 1500 all"],
                                   [f"C{i}" for i in range(6)]])
def test_freeze_refuses_bad_selections(tmp_path, names):
    with pytest.raises(SpecUndefinedError):
        holdout.freeze(names, UNIVERSE, tmp_path / "m.json")


def test_changed_code_fails_the_manifest_gate(tmp_path):
    path = tmp_path / "manifest.json"
    holdout.freeze(["C5-A 069500"], UNIVERSE, path)
    manifest = json.loads(path.read_text())
    manifest["code"]["tools/intraday_engine.py"] = "0" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(SpecUndefinedError, match="changed since the freeze"):
        holdout.verified(path)


def test_holdout_runs_once_trades_only_inside_the_window_and_judges(tmp_path, monkeypatch):
    dev_days = weekdays(date(2026, 5, 25), 26)            # ends 2026-06-29/30
    hold_days = weekdays(date(2026, 7, 1), 10)
    bars = tmp_path / "bars"
    write_bars(bars, UNIVERSE, [d for d in dev_days if d <= "2026-06-30"] + hold_days)
    path = tmp_path / "manifest.json"
    holdout.freeze(["C5-A 069500"], UNIVERSE, path)
    record = tmp_path / "holdout-run.json"
    result = holdout.run(path, bars, record)
    row = result["rows"][0]
    assert result["days"] == [hold_days[0], hold_days[-1]] and row["days"] == 10
    assert min(t["day"] for t in result["trades"]["C5-A 069500"]) >= "2026-07-01"
    # Rising prices: overnight gains are positive; the verdict follows §4.
    assert row["net_return"] > 0 and set(row["checks"]) == {"net_positive", "t_at_least_1_5", "mdd_within_etf"}
    assert row["passed"] == all(row["checks"].values())
    assert json.loads(record.read_text())["manifest_sha256"] == result["manifest_sha256"]
    with pytest.raises(SpecUndefinedError, match="already ran"):
        holdout.run(path, bars, record)


def test_holdout_needs_a_manifest(tmp_path):
    with pytest.raises(SpecUndefinedError, match="manifest"):
        holdout.run(tmp_path / "missing.json", tmp_path, tmp_path / "r.json")


def test_judgement_thresholds():
    ok = {"net_return": 0.01, "t": 1.5, "max_drawdown": 0.10}
    assert holdout.judge(ok, etf_mdd=0.10)["passed"]
    assert not holdout.judge({**ok, "net_return": 0.0}, etf_mdd=0.10)["passed"]
    assert not holdout.judge({**ok, "t": 1.49}, etf_mdd=0.10)["passed"]
    assert not holdout.judge({**ok, "max_drawdown": 0.11}, etf_mdd=0.10)["passed"]


def test_overnight_gets_report_only_comparisons_that_are_not_judged(tmp_path):
    dev_days = weekdays(date(2026, 5, 25), 26)
    hold_days = weekdays(date(2026, 7, 1), 10)
    bars = tmp_path / "bars"
    write_bars(bars, UNIVERSE, [d for d in dev_days if d <= "2026-06-30"] + hold_days)
    path = tmp_path / "manifest.json"
    holdout.freeze(["C5-A 069500"], UNIVERSE, path)
    row = holdout.run(path, bars, tmp_path / "r.json")["rows"][0]
    compare = row["report_only"]
    assert set(compare) == {"intraday_only", "full_day"}
    assert all("passed" not in c and "checks" not in c for c in compare.values())
    # Prices only move overnight (+10 a day): holding through the day keeps the gaps, intraday-only pays costs.
    assert compare["intraday_only"]["net_return"] < 0 < compare["full_day"]["net_return"]
    assert compare["full_day"]["net_return"] > row["net_return"]


def test_comparisons_hold_the_candidate_weights():
    overnight = cand.Overnight(("069500", "229200"))
    compare = holdout.comparisons(overnight)
    intraday, full = compare["intraday_only"], compare["full_day"]
    assert intraday.open_auction(None) == full.open_auction(None) == {"069500": 0.25, "229200": 0.25}
    assert intraday.close_auction(None) is None and full.close_auction(None) == {"069500": 0.25, "229200": 0.25}
    assert holdout.comparisons(cand.GapReversal("069500", 0.01)) == {}
