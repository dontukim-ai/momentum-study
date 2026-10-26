"""Market trend filter through the common evaluator (strategy-candidates-v8 C1)."""

from dataclasses import replace
from decimal import Decimal as D
from itertools import pairwise

from test_agent import market, policy
from trading_agent.data import Snapshot
from trading_agent.selection import RankedStrategy, SecurityHistory

from momentum.agent.evaluator import CommonEvaluator


def trend_market(drop_at=380):
    data = market(460)
    fund = [
        replace(b, symbol="FUND", open=price, high=price, low=price, close=price)
        for i, b in enumerate(data.snapshot.bars)
        for price in [D(10000) + 20 * i if i < drop_at else D(9000)]
    ]
    states = [replace(s, symbol="FUND", asset_type="ETF") for s in data.securities.records]
    return replace(
        data,
        snapshot=Snapshot([*data.snapshot.bars, *fund]),
        securities=SecurityHistory([*data.securities.records, *states]),
    )


def test_trend_strategy_holds_only_the_etf_and_exits_below_its_average():
    data = trend_market()
    p = policy()
    p = replace(
        p,
        decision_cadence="monthly",
        logic=replace(p.logic, selection=replace(p.logic.selection, market_symbol="FUND")),
    )
    spec = RankedStrategy(20, 1, D(".4"), ranking="market_trend")
    start, end = data.sessions[250], data.sessions[-1]
    result = CommonEvaluator(p).evaluate(data, spec, start, end)
    assert {t["symbol"] for t in result.trade_log} == {"FUND"}
    sides = [t["side"] for t in result.trade_log]
    assert sides[0] == "BUY" and "SELL" in sides
    by_session = {row["session"]: row["positions"].get("FUND", 0) for row in result.equity_curve}
    assert by_session[data.sessions[370].isoformat()] > 0
    assert by_session[end.isoformat()] == 0
    # Monthly sells before the drop only trim back to the 40% weight. The
    # exit waits for the first session of the month after the drop.
    sessions = [row["session"] for row in result.equity_curve]
    exit_day = next(d for prev, d in pairwise(sessions) if by_session[prev] > 0 and by_session[d] == 0)
    drop = data.sessions[380].isoformat()
    assert exit_day > drop
    first_of_month = [d for prev, d in pairwise(sessions) if d[:7] != prev[:7]]
    assert exit_day == next(d for d in first_of_month if d > drop)
    assert all(by_session[d] > 0 for d in sessions if first_of_month[0] <= d < exit_day)


def test_residual_momentum_reads_the_market_symbol_through_the_evaluator():
    # FIXTURE cycles +-2% every 10 sessions; FUND trends up. Residual momentum
    # needs FUND's bars in the evaluator's view to score FIXTURE at all.
    data = trend_market(drop_at=10**6)
    p = policy()
    p = replace(p, logic=replace(p.logic, selection=replace(p.logic.selection, market_symbol="FUND")))
    spec = RankedStrategy(20, 1, D(".4"), ranking="residual_momentum")
    result = CommonEvaluator(p).evaluate(data, spec, data.sessions[250], data.sessions[300])
    assert result.trades > 0
    assert {t["symbol"] for t in result.trade_log} <= {"FIXTURE", "FUND"}
    missing = replace(p, logic=replace(p.logic, selection=replace(p.logic.selection, market_symbol="NONE")))
    assert CommonEvaluator(missing).evaluate(data, spec, data.sessions[250], data.sessions[300]).trades == 0
