"""tools/allocation_rebalance.py on synthetic daily ETF prices (v14)."""

import importlib.util
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("allocation_rebalance", ROOT / "tools/allocation_rebalance.py")
alloc = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = alloc
spec.loader.exec_module(alloc)
Bar = alloc.Bar


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def market(n=120, drift=None, start=date(2024, 1, 2)):
    """Two ETFs: A drifts up 1% a session (open = previous close), B is flat; huge volume."""
    drift = drift or {"A": Decimal("1.01"), "B": Decimal(1)}
    prices, level = {}, {"A": Decimal(10000), "B": Decimal(10000)}
    for d in weekdays(start, n):
        row = {}
        for s in ("A", "B"):
            close = (level[s] * drift[s]).quantize(Decimal(1))
            row[s] = Bar(open=level[s], close=close, volume=1e9)
            level[s] = close
        prices[d] = row
    return prices


W = {"A": 0.5, "B": 0.5}


def test_rules_trigger_on_the_calendar_and_on_drift():
    d = alloc.decide
    jan31, feb1, apr1, jan2 = date(2024, 1, 31), date(2024, 2, 1), date(2024, 4, 1), date(2025, 1, 2)
    now = {"A": 0.5, "B": 0.5}
    assert d("M", jan31, feb1, now, W) and not d("Q", jan31, feb1, now, W) and not d("A", jan31, feb1, now, W)
    assert d("Q", date(2024, 3, 29), apr1, now, W) and d("A", date(2024, 12, 31), jan2, now, W)
    assert not d("M", feb1, date(2024, 2, 2), now, W) and not d("H", jan31, feb1, now, W)
    assert not d("B5", feb1, feb1, {"A": 0.549, "B": 0.451}, W)
    assert d("B5", feb1, feb1, {"A": 0.551, "B": 0.449}, W) and not d("B10", feb1, feb1, {"A": 0.551, "B": 0.449}, W)
    with pytest.raises(SpecUndefinedError):
        d("W", jan31, feb1, now, W)


def test_buy_and_hold_trades_once_and_the_ledger_reconciles():
    prices = market()
    result = alloc.run(prices, W, "H", cost_bps=4.5)
    assert result["rebalances"] == [] and result["measure_from"] == 1  # deep volume: built on the first session
    last = result["sessions"][-1]
    nav = result["ledger"].cash + sum(q * prices[last][s].close for s, q in result["ledger"].positions.items())
    assert result["nav"][-1] == nav                                      # exact, whole won
    assert result["drift"][-1] > result["drift"][0]                      # A ran away from 50%


def test_monthly_trades_each_month_and_bands_trade_only_on_drift():
    prices = market()
    first = alloc.run(prices, W, "M", cost_bps=4.5)
    months = {d.strftime("%Y-%m") for d in first["sessions"]}
    assert len(first["rebalances"]) == len(months) - 1                 # the build month has no rule rebalance
    band = alloc.run(prices, W, "B5", cost_bps=4.5)
    # 1% a session on A moves its weight ~0.25pp a session: about 20 sessions to cross 5pp.
    gaps = [(b - a).days for a, b in zip(band["rebalances"], band["rebalances"][1:], strict=False)]
    assert len(band["rebalances"]) >= 3 and min(gaps) >= 20
    assert max(band["drift"]) < 0.13                                     # never far past the band
    assert alloc.run(prices, W, "B10", cost_bps=4.5)["rebalances"].__len__() < len(band["rebalances"])


def test_trading_waits_for_twenty_sessions_of_volume_history():
    prices = market(30)
    result = alloc.run(prices, W, "H", cost_bps=4.5)
    assert result["sessions"][0] == sorted(prices)[20]


def test_missing_open_cancels_the_buy_and_missing_close_keeps_the_mark():
    prices = market()
    start = sorted(prices)[20]
    prices[start]["B"] = Bar(open=None, close=prices[start]["B"].close, volume=1e9)
    result = alloc.run(prices, W, "H", cost_bps=4.5)
    assert any(f.status == "cancelled_no_open" for f in result["ledger"].fills)
    # v14.1: the cancelled buy keeps the build open, so B is bought at the next open.
    assert result["measure_from"] == 2 and result["ledger"].positions.get("B", 0) > 0


def test_an_implausible_close_stops_the_run():
    prices = market()
    day = sorted(prices)[50]
    prices[day]["A"] = Bar(open=prices[day]["A"].open, close=prices[day]["A"].open / 2, volume=1e9)
    with pytest.raises(SpecUndefinedError, match="implausible"):
        alloc.run(prices, W, "M", cost_bps=4.5)


def test_metrics_and_halves():
    result = alloc.run(market(), W, "M", cost_bps=4.5)
    m = alloc.metrics(result)
    assert set(m) == {"full", "first_half", "second_half"}
    full = m["full"]
    assert full["rebalances"] == len(result["rebalances"])              # build sessions are not counted
    assert full["cost_bp_per_year"] > 0 and full["turnover_per_year"] > 0 and 0 <= full["max_drawdown"] < 0.05


def test_judgement_keeps_annual_unless_a_rule_wins_everywhere():
    def row(drift, mdd, cost):
        return {p: {"drift_mean": drift, "max_drawdown": mdd, "cost_bp_per_year": cost}
                for p in ("full", "first_half", "second_half")}
    table = {"A": row(0.05, 0.20, 2.0), "M": row(0.02, 0.19, 9.0), "Q": row(0.03, 0.20, 4.0),
             "B5": row(0.03, 0.21, 3.0), "H": row(0.20, 0.25, 0.0), "B10": row(0.06, 0.20, 1.0)}
    verdict = alloc.judge(table)
    assert verdict["passing"] == ["Q"] and verdict["choice"] == "Q"     # M costs 7bp more, B5 deeper drawdown
    table["Q"]["second_half"]["drift_mean"] = 0.06
    assert alloc.judge(table)["choice"] == "A"


def test_fsc_pages_load_open_close_volume(tmp_path):
    day = tmp_path / "etf" / "2024-01-02"
    day.mkdir(parents=True)
    page = {"response": {"body": {"items": {"item": [
        {"srtnCd": "A", "mkp": "10000", "clpr": "10100", "trqu": "500"},
        {"srtnCd": "B", "mkp": "0", "clpr": "9000", "trqu": "0"},
        {"srtnCd": "Z", "mkp": "1", "clpr": "1", "trqu": "1"}]}}}}
    (day / "page-1.json").write_text(json.dumps(page))
    (tmp_path / "etf" / ".DS_Store").write_text("")
    prices = alloc.load_fsc(tmp_path, ("A", "B"))
    bars = prices[date(2024, 1, 2)]
    assert bars["A"] == Bar(Decimal(10000), Decimal(10100), 500.0)
    assert bars["B"].open is None and set(bars) == {"A", "B"}


def thin(prices, volume):
    return {d: {s: b._replace(volume=volume) for s, b in row.items()} for d, row in prices.items()}


def test_build_up_buys_every_session_until_nothing_is_cut_then_rules_apply():
    # 1% of 30,000 shares = 300 a session; 50M won per ETF at ~10,000+ needs about 17 sessions.
    prices = thin(market(), 30_000)
    result = alloc.run(prices, W, "H", cost_bps=4.5)
    built = result["measure_from"]
    assert built > 10
    assert result["rebalances"] == []                                   # H never rebalances after the build
    assert result["drift"][built - 1] < 0.02                            # fully invested at the end of the build
    assert all(f.status == "filled" for f in result["ledger"].fills if f.session_date == result["sessions"][built - 1])
    m = alloc.run(prices, W, "M", cost_bps=4.5)
    assert m["measure_from"] == built                                    # every rule shares the build


def test_a_build_that_never_completes_stops():
    with pytest.raises(SpecUndefinedError, match="build"):
        alloc.run(thin(market(), 1_000), W, "H", cost_bps=4.5)


def test_metrics_start_after_the_build():
    result = alloc.run(thin(market(), 30_000), W, "M", cost_bps=4.5)
    m = alloc.metrics(result)
    assert m["full"]["sessions"] == len(result["sessions"]) - result["measure_from"]
    assert m["first_half"]["sessions"] + m["second_half"]["sessions"] == m["full"]["sessions"]
