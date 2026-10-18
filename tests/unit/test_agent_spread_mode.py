"""Synthetic evidence for preregistered missing-spread research."""

from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_agent_release import market, policy, staged
from test_agent_tradability import capacity_market, evaluate
from trading_agent.contracts import AgentError, digest, session_time
from trading_agent.selection import SecurityHistory
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, policy_from_dict
from momentum.agent.service import ResearchAgent


def historical(p, slippage=D(25)):
    return replace(
        p,
        logic=replace(
            p.logic,
            selection=replace(p.logic.selection, spread_mode="historical_unavailable"),
            costs=replace(p.logic.costs, slippage_bps=slippage),
        ),
    )


def test_historical_cost_floor():
    p = AgentPolicy()
    for slippage in (D(5), D("24.999")):
        with pytest.raises(
            AgentError,
            match="Historical spread-unavailable mode requires preregistered slippage of at least 25 bps",
        ):
            historical(p, slippage)
    assert historical(p).logic.costs.slippage_bps == D(25)
    assert historical(p, D(30)).logic.costs.slippage_bps == D(30)
    assert (
        policy_from_dict({"logic": {"selection": {}}}).logic.selection.spread_mode
        == "quoted"
    )


def test_mode_is_bound_to_selection_digest_and_contract():
    data, p, spec = capacity_market()
    p = replace(
        p, logic=replace(p.logic, costs=replace(p.logic.costs, slippage_bps=D(25)))
    )
    a, b = evaluate(data, p, spec), evaluate(data, historical(p), spec)
    assert (
        a.evaluation_contract["data_bundle_id"]
        == b.evaluation_contract["data_bundle_id"]
    )
    assert (
        a.evaluation_contract["cost_scenario"] == b.evaluation_contract["cost_scenario"]
    )
    assert a.evaluation_contract["selection_policy_id"] == digest(p.logic.selection)
    assert b.evaluation_contract["selection_policy_id"] == digest(
        historical(p).logic.selection
    )
    assert a.evaluation_contract_id != b.evaluation_contract_id
    assert "spread_mode" not in b.portfolio_diagnostics


def test_missing_quotes_cash_or_hand_calculated_25bps_fill():
    data, p, spec = capacity_market()
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, bid=None, ask=None) for s in data.securities.records]
        ),
    )
    quoted = evaluate(data, p, spec)
    assert not quoted.trade_log
    assert all(D(row["equity"]) == D(1000000) for row in quoted.equity_curve)
    result = evaluate(data, historical(p), spec)
    first = result.trade_log[0]
    assert first["side"].lower() == "buy"
    assert D(first["open_price"]) == D(4000)
    # 4,000 * (1 + 25 / 10,000) = 4,010 KRW; zero fees here.
    assert D(first["price"]) == D(4010)
    assert D(first["fee"]) == D(0)
    assert first["quantity"] == 99
    filled_day = next(
        row for row in result.equity_curve if row["session"] == first["session"]
    )
    assert D(filled_day["equity"]) == D(1000000) - D(99) * D(10)


@pytest.mark.parametrize(
    "mode,eligible", [("quoted", True), ("historical_unavailable", False)]
)
def test_e2_release_execution_eligibility(tmp_path, mode, eligible):
    data, p = market(284), policy()
    if mode == "historical_unavailable":
        p = historical(p)
        data = replace(
            data,
            securities=SecurityHistory(
                [replace(s, bid=None, ask=None) for s in data.securities.records]
            ),
        )
    now = [session_time(staged(data, p).sessions[-1], 17)]
    store = Store(tmp_path / "spread.sqlite3")
    try:
        agent = ResearchAgent(store, clock=lambda: now[0])
        agent.init("spread", p)
        agent.tick("spread", staged(data, p))
        now[0] = session_time(data.sessions[-1], 17)
        report = agent.tick("spread", data)
        assert report["cycles"][0]["research_champion_updated"]
        assert report["status"]["policy"]["logic"]["selection"]["spread_mode"] == mode
        release = agent.register_observation_release(approved_by="reviewer")
        assert release["evidence_grade"] == "E2"
        assert release["spread_mode"] == mode
        assert release["execution_eligible"] is eligible
        assert release["order_authority"] is False
        assert agent.register_observation_release(approved_by="reviewer") == release
    finally:
        store.close()
