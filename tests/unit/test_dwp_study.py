"""tools/dwp_study.py (v31) on synthetic inputs, targets computed by hand."""

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t
from trading_agent.fundamentals import FundamentalHistory, ShareCount

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

spec = importlib.util.spec_from_file_location("dwp_study", ROOT / "tools/dwp_study.py")
dw = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = dw
spec.loader.exec_module(dw)

N = 220
PRICES = {"005930": 100.0, "000660": 50.0, "035420": 10.0, "069500": 10000.0}
SHARES = {"005930": 4_000_000, "000660": 2_000_000, "035420": 1_000_000}   # caps 4e8, 1e8, 1e7


def share_history(sessions, counts=SHARES):
    since = KRX.historical_bar_at(sessions[0])
    return FundamentalHistory([], [ShareCount(s, sessions[0], sessions[-1], since, n, "test")
                                   for s, n in counts.items()])


def flat():
    return t.make_data({s: [px] * N for s, px in PRICES.items()}, n=N)


def test_weights_by_hand():
    caps = {"a": 4.0, "b": 1.0}
    assert dw.weights(caps, 1.0) == pytest.approx({"a": 0.8, "b": 0.2})
    assert dw.weights(caps, 0.0) == pytest.approx({"a": 0.5, "b": 0.5})
    assert dw.weights(caps, 0.5) == pytest.approx({"a": 2 / 3, "b": 1 / 3})     # sqrt(.8) : sqrt(.2) = 2 : 1


def test_decisions_are_the_last_sessions_of_quarter_months():
    _, sessions = flat()
    assert [sessions[k] for k in dw.decision_days(sessions)] == [
        date(2020, 3, 31), date(2020, 6, 30), date(2020, 9, 30)]


def test_universe_leaves_out_stocks_without_shares_and_stops_when_short():
    data, sessions = flat()
    k = sessions.index(date(2020, 3, 31))
    today = {b.symbol: b for b in data.snapshot.bars if b.session == sessions[k]}
    at = dw.ma._decision(sessions[k])
    from momentum.krx.market_events import MarketEvents
    none = MarketEvents([])
    chosen, diag = dw.universe(today, data.securities.states(at), share_history(sessions), none, sessions[k], at, 2)
    assert chosen == {"005930": 4e8, "000660": 1e8} and diag["mu_max"] == pytest.approx(0.8)
    assert diag["top2"] == pytest.approx(1.0) and diag["missing_shares"] == 0
    partial = share_history(sessions, {"005930": 4_000_000, "000660": 2_000_000})
    assert dw.universe(today, data.securities.states(at), partial, none, sessions[k], at, 2)[1]["missing_shares"] == 1
    with pytest.raises(SpecUndefinedError):
        dw.universe(today, data.securities.states(at), partial, none, sessions[k], at, 3)


@pytest.mark.parametrize(("p", "expected"), [(1.0, {"005930": 760_000, "000660": 380_000}),
                                             (0.0, {"005930": 475_000, "000660": 950_000})])
def test_first_rebalance_buys_95_percent_of_nav_at_the_next_open(p, expected):
    data, sessions = flat()
    result = dw.backtest(data, share_history(sessions), p, 36.5, top=2)
    first = {f.security_id: f.shares for f in result["fills"] if f.session_date == date(2020, 4, 1)}
    assert first == expected                                     # 0.95 x 1e8 x weight / prior close, floored
    assert result["counts"]["rebalances"] == 3 and result["days"][0] == date(2020, 4, 1)
    later = [f for f in result["fills"] if f.session_date > date(2020, 4, 1) and f.shares > 0]
    assert all(f.side == "sell" for f in later)                  # flat prices: costs shrink NAV, targets trim


def test_a_quarantined_holding_is_sold_and_not_bought_back(tmp_path):
    data, sessions = flat()
    filed = date(2020, 4, 15)
    market = t._events(("005930", "restructuring", filed))
    result = dw.backtest(data, share_history(sessions), 1.0, 36.5, market, top=2)
    sells = [f for f in result["fills"] if f.security_id == "005930" and f.side == "sell" and f.shares > 0]
    assert [(f.session_date, f.shares) for f in sells] == [(sessions[sessions.index(filed) + 1], 760_000)]
    assert result["counts"]["quarantine_exits"] == 1
    assert not [f for f in result["fills"] if f.security_id == "005930" and f.side == "buy"
                and f.session_date > filed and f.shares > 0]
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        dw.main(["--input", str(tmp_path), "--events", "e", "--shares", "s", "--record", str(record)])


def test_study_judges_three_variants_and_reports_the_contrast():
    data, sessions = flat()
    result = dw.study(data, share_history(sessions), top=2)
    assert set(result["judged"]) == {"dwp_p0", "dwp_p05", "dwp_p08"} and set(result["runs"]) == set(dw.VARIANTS)
    assert result["judged"]["dwp_p0"]["e1_pass"] is False        # no second half in a 2020-only input
    assert len(result["diagnostics"]) == 3 and "cap_p1" in dw.text(result)


def test_the_benchmark_books_069500s_own_distributions():
    from decimal import Decimal
    from types import SimpleNamespace

    from momentum.ledger.ledger import CorporateAction
    _, sessions = flat()
    paid = SimpleNamespace(action=CorporateAction("069500", "cash_dividend", date(2020, 5, 4), date(2020, 5, 20),
                                                  None, Decimal(100)),
                           available_at=datetime.combine(date(2020, 4, 20), datetime.min.time(), t.KST))
    plain, _ = flat()
    with_div, _ = t.make_data({s: [px] * N for s, px in PRICES.items()}, actions=[paid], n=N)
    gap = (dw.backtest(with_div, share_history(sessions), 1.0, 36.5, top=2)["bench"][-1]
           - dw.backtest(plain, share_history(sessions), 1.0, 36.5, top=2)["bench"][-1])
    assert gap == pytest.approx(9_500 * 100)                    # 0.95 x 1e8 / 10,000 units x 100 won
