"""tools/ma_screen.py on synthetic FSC pages (no network, no real data)."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ma_screen", ROOT / "tools/ma_screen.py")
screen = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = screen
spec.loader.exec_module(screen)


def weekdays(n, start=date(2026, 1, 5)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def write(root, paths, n, names=None, bases=None, value=None, skip=()):
    """paths: code -> closes; bases: (code, k) -> exchange base price on session k."""
    days = weekdays(n)
    for k, day in enumerate(days):
        items = []
        for code, closes in paths.items():
            if (code, k) in skip:
                continue
            prev = closes[k - 1] if k else closes[0]
            base = (bases or {}).get((code, k), prev)
            items.append({"srtnCd": code, "itmsNm": (names or {}).get(code, f"종목{code}"), "mrktCtg": "KOSPI",
                          "clpr": str(closes[k]), "vs": str(closes[k] - base),
                          "trPrc": str((value or {}).get(code, 1e10))})
        folder = root / "stock" / day
        folder.mkdir(parents=True)
        (folder / "page-0001.json").write_text(json.dumps({"response": {"body": {"items": {"item": items}}}}))
    return days


def test_golden_cross_today_is_reported_with_its_averages(tmp_path):
    n = 130
    flat_then_up = [100.0] * (n - 1) + [400.0]          # MA20 jumps above a still-flat MA60 on the last day
    days = write(tmp_path, {"005930": flat_then_up, "000660": [100.0] * n}, n)
    result = screen.screen(screen.load(tmp_path))
    assert result["day"] == days[-1] and result["universe"] == 2
    assert [r["code"] for r in result["golden"]] == ["005930"]
    row = result["golden"][0]
    assert row["ma20"] == pytest.approx((19 * 100 + 400) / 20) and row["ma60"] == pytest.approx((59 * 100 + 400) / 60)
    assert result["dead"] == []


def test_a_split_is_not_a_dead_cross(tmp_path):
    n = 130
    closes = [500.0 + k for k in range(n - 1)] + [(500.0 + n - 1) / 5]
    split_day = n - 1
    base = (500.0 + n - 2) / 5                            # the exchange's base after a 1:5 split
    write(tmp_path, {"005930": closes}, n, bases={("005930", split_day): base})
    result = screen.screen(screen.load(tmp_path))
    assert result["dead"] == [] and result["adjusted"] == 1


def test_only_the_top_by_trading_value_with_full_history_are_screened(tmp_path):
    n = 130
    paths = {"005930": [100.0] * n, "000660": [100.0] * n, "035420": [100.0] * n}
    write(tmp_path, paths, n, value={"005930": 3e10, "000660": 2e10, "035420": 1e10},
          skip={("000660", 50)})
    result = screen.screen(screen.load(tmp_path), top=1)
    assert result["universe"] == 1 and result["short_history"] == 1


def test_preferred_shares_spacs_and_holidays_are_skipped(tmp_path):
    assert screen.common_share("005930", "삼성전자", "KOSPI")
    assert not screen.common_share("005935", "삼성전자우", "KOSPI")
    assert not screen.common_share("123450", "OO스팩1호", "KOSDAQ")
    assert not screen.common_share("005930", "삼성전자", "KONEX")
    days = write(tmp_path, {"005930": [100.0] * 125}, 125)
    (tmp_path / "stock" / "2027-01-01").mkdir()          # an empty folder: a holiday
    loaded = screen.load(tmp_path)
    assert list(loaded)[-1] == days[-1] and len(loaded) == 121


def test_too_little_history_is_refused(tmp_path):
    write(tmp_path, {"005930": [100.0] * 50}, 50)
    with pytest.raises(screen.SpecUndefinedError):
        screen.screen(screen.load(tmp_path))
