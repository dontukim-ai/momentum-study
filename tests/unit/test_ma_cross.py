"""tools/ma_cross.py on synthetic sessions (no network, no real data)."""

import importlib.util
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from momentum.ledger.ledger import CorporateAction

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ma_cross", ROOT / "tools/ma_cross.py")
ma = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ma
spec.loader.exec_module(ma)


def test_moving_averages_cross_and_alignment_by_hand():
    assert ma.sma([1, 2, 3, 4], 2) == 3.5 and ma.sma([1, 2], 3) is None
    assert ma.cross(9, 10, 11, 10) == "golden"
    assert ma.cross(10, 10, 11, 10) == "golden"      # touching then above counts once
    assert ma.cross(11, 10, 9, 10) == "dead"
    assert ma.cross(11, 10, 12, 10) is None
    assert ma.alignment([5, 4, 3, 2]) == "bull" and ma.alignment([2, 3, 4, 5]) == "bear"
    assert ma.alignment([5, 4, 4, 2]) is None


def test_adjust_divides_closes_before_an_action():
    # a 1:5 split on session 2: 500 -> 100 is no move once 500 becomes 100
    assert ma.adjust([500, 510, 100, 104], [1, 1, 5, 1]) == [100, 102, 100, 104]
    assert ma.adjust([10, 12], [1, 1]) == [10, 12]


def test_screen_signals_need_121_closes_and_report_new_alignment():
    rising = [100 + k for k in range(121)]
    sig = ma.signals(rising)
    assert sig["alignment"] == "bull" and sig["entered"] is None   # already aligned yesterday
    with pytest.raises(ma.SpecUndefinedError):
        ma.signals(rising[:120])


def test_action_factors_match_the_ledger():
    split = CorporateAction("A", "split", date(2020, 5, 4), None, Decimal(5), None)
    bonus = CorporateAction("A", "bonus_issue", date(2020, 5, 4), date(2020, 5, 20), Decimal("0.5"), None)
    cut = CorporateAction("A", "capital_reduction", date(2020, 5, 4), None, Decimal("0.1"), None)
    cash = CorporateAction("A", "cash_dividend", date(2020, 5, 4), date(2020, 5, 20), None, Decimal(100))
    assert [ma.action_factor(a) for a in (split, bonus, cut, cash)] == [5.0, 1.5, 0.1, None]


KST = ma.KST


def weekdays(n, start=date(2020, 1, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def path_down_up_down(n):
    """200 -> 110 over 90 sessions, back to 200 over 60, down again: one golden, then one dead cross."""
    out = []
    for k in range(n):
        if k < 90:
            out.append(200 - k)
        elif k < 150:
            out.append(110 + (k - 90) * 1.5)
        else:
            out.append(200 - (k - 150) * 2)
    return out


def state(symbol, asset="STOCK", managed=False, status="NORMAL"):
    return SimpleNamespace(symbol=symbol, status=status, asset_type=asset, market="KOSPI", managed=managed)


def make_data(paths, states=None, actions=(), n=220):
    sessions = weekdays(n)
    bars = []
    for sym, path in paths.items():
        for k, d in enumerate(sessions):
            px = Decimal(str(path[k]))
            bars.append(SimpleNamespace(session=d, symbol=sym, open=px, close=px, volume=100_000_000))
    states = states or {s: state(s, "ETF" if s == "069500" else "STOCK") for s in paths}
    securities = SimpleNamespace(states=lambda at: dict(states))
    return SimpleNamespace(sessions=tuple(sessions), snapshot=SimpleNamespace(bars=bars),
                           actions=tuple(actions), securities=securities), sessions


def golden_and_dead_days(path, sessions):
    days = {}
    for k in range(ma.LONG + 1, len(path)):
        sig = ma.cross(ma.sma(path[:k], 20), ma.sma(path[:k], 60), ma.sma(path[:k + 1], 20), ma.sma(path[:k + 1], 60))
        if sig and sig not in days:
            days[sig] = k
    return days


def test_buys_the_open_after_a_golden_cross_and_sells_the_open_after_the_dead_cross():
    path = path_down_up_down(220)
    data, sessions = make_data({"005930": path, "069500": [10000] * 220})
    marks = golden_and_dead_days(path, sessions)
    assert sessions[marks["golden"]] >= ma.FIRST_TRADE
    result = ma.backtest(data, 36.5)
    fills = [f for f in result["fills"] if f.security_id == "005930" and f.shares > 0]
    buy, sell = fills[0], fills[-1]
    assert buy.side == "buy" and buy.session_date == sessions[marks["golden"] + 1]
    # sized on the golden-cross close: 5% of NAV (less the 0.5% buffer) at that close
    gc_close = Decimal(str(path[marks["golden"]]))
    assert buy.shares == int(Decimal(100_000_000) * Decimal("0.995") * Decimal("0.05") // gc_close)
    assert sell.side == "sell" and sell.session_date == sessions[marks["dead"] + 1] and sell.shares == buy.shares
    assert result["counts"]["dead_exits"] == 1
    bench = [f for f in result["fills"] if f.security_id == "069500"]
    assert bench == []                                # the benchmark keeps its own ledger


def test_a_split_is_no_dead_cross_and_the_ledger_scales_the_holding():
    path = path_down_up_down(220)
    marks = golden_and_dead_days(path, weekdays(220))
    split_at = marks["golden"] + 5
    raw = [p / 5 if k >= split_at else p for k, p in enumerate(path)]
    sessions = weekdays(220)
    split = SimpleNamespace(action=CorporateAction("005930", "split", sessions[split_at], None, Decimal(5), None),
                            available_at=datetime.combine(sessions[split_at - 10], datetime.min.time(), KST))
    data, _ = make_data({"005930": raw, "069500": [10000] * 220}, actions=[split])
    result = ma.backtest(data, 36.5)
    fills = [f for f in result["fills"] if f.security_id == "005930" and f.shares > 0]
    assert [f.side for f in fills] == ["buy", "sell"]
    assert fills[1].session_date == sessions[marks["dead"] + 1]
    assert fills[1].shares == fills[0].shares * 5


def test_managed_or_non_stock_names_are_never_bought():
    path = path_down_up_down(220)
    states = {"005930": state("005930", managed=True), "069500": state("069500", "ETF")}
    data, _ = make_data({"005930": path, "069500": path}, states=states)
    result = ma.backtest(data, 36.5)
    assert not [f for f in result["fills"] if f.shares > 0]


def test_report_needs_both_halves():
    days = weekdays(1800, date(2020, 4, 1))
    up = [1.0005 ** k for k in range(len(days))]
    flat = [1.0] * len(days)
    rep = ma.report({"days": days, "nav": up, "bench": flat, "fills": [], "counts": {}, "fractions_dropped": 0})
    assert rep["e1_pass"] is True
    rep = ma.report({"days": days, "nav": flat, "bench": up, "fills": [], "counts": {}, "fractions_dropped": 0})
    assert rep["e1_pass"] is False


def test_a_long_suspension_defers_the_sell_until_trading_resumes():
    path = path_down_up_down(220)
    sessions = weekdays(220)
    marks = golden_and_dead_days(path, sessions)
    gap = range(marks["golden"] + 5, marks["golden"] + 30)          # 25 sessions without bars while held
    states = {"005930": state("005930"), "069500": state("069500", "ETF")}
    data, _ = make_data({"005930": path, "069500": [10000] * 220}, states=states)
    data.snapshot.bars = [b for b in data.snapshot.bars
                          if not (b.symbol == "005930" and sessions.index(b.session) in gap)]
    suspended = {"005930": state("005930", status="SUSPENDED"), "069500": state("069500", "ETF")}
    first, last = sessions[gap[0]], sessions[gap[-1]]
    data.securities = SimpleNamespace(
        states=lambda at: suspended if first <= at.date() <= last else states)
    result = ma.backtest(data, 36.5)                                 # no "no ADV data" error
    fills = [f for f in result["fills"] if f.security_id == "005930" and f.shares > 0]
    assert [f.side for f in fills] == ["buy", "sell"]
    # the first resumed close gives the order a volume to size against; it fills at the next open
    assert fills[1].session_date == sessions[gap[-1] + 2]
    assert result["counts"]["orders_waiting_no_adv"] > 0
