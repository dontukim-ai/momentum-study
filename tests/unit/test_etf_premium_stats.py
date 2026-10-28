"""tools/etf_premium_stats.py: descriptive close-vs-NAV premium statistics on fake FSC pages."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("etf_premium_stats", ROOT / "tools/etf_premium_stats.py")
stats = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = stats
spec.loader.exec_module(stats)


def page(root, day, rows):
    folder = root / "etf" / day
    folder.mkdir(parents=True)
    items = [{"srtnCd": c, "itmsNm": n, "clpr": str(p), "nav": str(v), "trqu": str(q), "trPrc": str(q * p)}
             for c, n, p, v, q in rows]
    (folder / "page-1.json").write_text(json.dumps({"response": {"body": {"items": {"item": items}}}}))


def test_groups_premiums_and_next_day_persistence(tmp_path):
    page(tmp_path, "2024-01-02", [("A", "KODEX 200", 10050, 10000, 10), ("B", "TIGER 미국S&P500", 9900, 10000, 10),
                                  ("C", "KODEX 국고채3년", 10000, 10000, 0), ("D", "KODEX 레버리지", 100, 0, 5)])
    page(tmp_path, "2024-01-03", [("A", "KODEX 200", 10010, 10000, 10), ("B", "TIGER 미국S&P500", 9950, 10000, 10)])
    rows = stats.load(tmp_path)
    first = [r for r in rows if r.day == "2024-01-02"]          # C has no volume, D no NAV
    assert [r.code for r in first] == ["A", "B"]
    assert [r.premium_bp for r in first] == pytest.approx([50.0, -100.0])
    assert stats.group("TIGER 미국S&P500") == "overseas" and stats.group("KODEX 국고채3년") == "bond"
    assert stats.group("KODEX 레버리지") == "leveraged_inverse" and stats.group("KODEX 200") == "domestic_equity"
    out = stats.summarize(rows, top_n=1)
    dom = out["groups"]["domestic_equity"]
    assert dom["etf_days"] == 2 and dom["abs_median_bp"] == pytest.approx(30.0)
    assert dom["share_abs_over"]["50"] == 0.0 and out["groups"]["overseas"]["share_abs_over"]["50"] == 1.0
    # Next day: A 50 -> 10 bp (kept 20%), B -100 -> -50 bp (kept 50%), both above the 30bp line.
    assert out["persistence"]["pairs"] == 2 and out["persistence"]["kept_ratio_median"] == pytest.approx(0.35)
    assert out["persistence"]["sign_flip_share"] == 0.0
    assert set(out["liquid"]) == {"top", "rest"}
