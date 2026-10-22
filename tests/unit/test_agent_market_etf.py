"""A fixed, preregistered ETF is a risk-capped reporting benchmark."""

import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_agent_tradability import capacity_market, evaluate
from trading_agent.contracts import AgentError, canonical
from trading_agent.data import Snapshot
from trading_agent.selection import SecurityHistory
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, policy_from_dict
from momentum.agent.evaluator import CommonEvaluator, require_same_contract
from momentum.agent.service import ResearchAgent, promotion_gates


def etf_market():
    data, p, spec = capacity_market(symbols=("STOCK", "FUND"))
    data = replace(
        data,
        securities=SecurityHistory(
            [
                replace(s, asset_type="ETF") if s.symbol == "FUND" else s
                for s in data.securities.records
            ]
        ),
    )
    return data, replace(p, market_etf_symbol="FUND"), spec


def test_fixed_etf_is_selected_without_stock_rank_and_keeps_same_contract():
    data, p, spec = etf_market()
    data = replace(
        data,
        snapshot=Snapshot(
            [
                replace(b, close=D(4040), high=D(4040))
                if b.symbol == "STOCK" and b.session == data.sessions[70]
                else b
                for b in data.snapshot.bars
            ]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    require_same_contract(evaluate(data, p, spec), r)
    assert {t["symbol"] for t in r.trade_log} == {"FUND"}
    assert r.trade_log[0]["planned_quantity"] == 100
    assert r.equity_curve[-1]["positions"] == {"FUND": 100}
    assert r.portfolio_diagnostics["average_invested_weight"] == 0.2
    assert r.evaluation_contract["benchmark_definitions"]


def test_market_etf_requires_explicit_configuration_and_legacy_json_defaults_off():
    data, p, _ = capacity_market()
    raw = json.loads(canonical(p))
    raw.pop("market_etf_symbol", None)
    assert policy_from_dict(raw).market_etf_symbol is None
    with pytest.raises(AgentError, match="configured"):
        CommonEvaluator(p).evaluate_benchmark(
            data, "market_etf", data.sessions[70], data.sessions[71]
        )


@pytest.mark.parametrize("symbol", ["", "bad/name", 123, True])
def test_etf_symbol_is_strict(symbol):
    with pytest.raises(AgentError):
        AgentPolicy(market_etf_symbol=symbol)


def test_stock_cannot_silently_substitute_for_market_etf():
    data, p, _ = etf_market()
    p = replace(p, market_etf_symbol="STOCK")
    with pytest.raises(AgentError, match="ETF"):
        CommonEvaluator(p).evaluate_benchmark(
            data, "market_etf", data.sessions[70], data.sessions[71]
        )


def test_etf_missing_open_cancels_without_substitute_or_forward_price_sizing():
    data, p, _ = etf_market()
    data = replace(
        data,
        snapshot=Snapshot(
            [
                b
                for b in data.snapshot.bars
                if not (b.symbol == "FUND" and b.session == data.sessions[71])
            ],
            require_complete=False,
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    assert not r.trade_log
    assert r.execution_events[0]["symbol"] == "FUND"
    assert r.execution_events[0]["status"] == "cancelled_no_open"
    assert r.tradability["fill_ratio"] == 0


def test_changed_etf_is_a_different_policy_and_benchmark_definition():
    data, p, _ = etf_market()
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, asset_type="ETF") for s in data.securities.records]
        ),
    )
    first = CommonEvaluator(p).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    other = CommonEvaluator(replace(p, market_etf_symbol="STOCK")).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    assert first.strategy_version != other.strategy_version
    with pytest.raises(AgentError, match="benchmark_definitions"):
        require_same_contract(first, other)


def test_etf_obeys_the_existing_order_cap_and_base_costs():
    from trading_agent.contracts import Costs

    data, p, _ = etf_market()
    p = replace(
        p,
        logic=replace(
            p.logic,
            costs=Costs(D(10), D(0), D(0)),
            selection=replace(p.logic.selection, max_order_notional=D(200000)),
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    assert r.trade_log[0]["planned_quantity"] == r.trade_log[0]["quantity"] == 50
    assert r.trade_log[0]["fee"] == r.fees == "200"
    assert r.net_return == -0.0002


def test_future_metadata_cannot_authorize_the_etf_reference():
    from trading_agent.contracts import session_time

    data, p, _ = etf_market()
    data = replace(
        data,
        securities=SecurityHistory(
            [
                replace(s, available_at=session_time(data.sessions[71], 16))
                if s.symbol == "FUND"
                else s
                for s in data.securities.records
            ]
        ),
    )
    with pytest.raises(AgentError, match="metadata unavailable"):
        CommonEvaluator(p).evaluate_benchmark(
            data, "market_etf", data.sessions[70], data.sessions[71]
        )


def test_etf_market_filters_are_reported_without_stock_substitution():
    data, p, _ = etf_market()
    data = replace(
        data,
        securities=SecurityHistory(
            [
                replace(s, managed=True) if s.symbol == "FUND" else s
                for s in data.securities.records
            ]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    assert r.trades == 0
    assert r.portfolio_diagnostics["average_cash_weight"] == 1
    assert r.portfolio_diagnostics["screening_rejection_sessions"] == 2


def test_configured_etf_is_reported_for_kept_cycle_and_shadow_without_voting(tmp_path):
    from test_agent import market, policy

    data = market(270, cycle=False)
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, asset_type="ETF") for s in data.securities.records]
        ),
    )
    p = replace(policy(), holdout_sessions=40, market_etf_symbol="FIXTURE")
    store = Store(tmp_path / "etf.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init("etf", p)
        report = agent.tick("etf", data)
        evidence = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        for summaries in (
            evidence["holdout"]["benchmarks"],
            report["shadow"]["benchmarks"],
        ):
            b = summaries["market_etf"]
            assert b["symbol"] == "FIXTURE"
            assert b["status"] == "EVALUATED"
            b["net_return"] = 100
        assert promotion_gates(evidence, p) == evidence["gates"]
        with pytest.raises(AgentError, match="Preserve"):
            agent.init("etf", replace(p, market_etf_symbol="OTHER"))
    finally:
        store.close()


def test_missing_etf_is_unavailable_in_report_instead_of_a_zero_return(tmp_path):
    from test_agent import market, policy

    store = Store(tmp_path / "missing-etf.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init(
            "missing",
            replace(policy(), holdout_sessions=40, market_etf_symbol="MISSING"),
        )
        report = agent.tick("missing", market(270, cycle=False))
        b = report["shadow"]["benchmarks"]["market_etf"]
        assert b["status"] == "UNAVAILABLE"
        assert "net_return" not in b
        assert "reason" in b
        assert report["status"]["revision"] == 0
    finally:
        store.close()


def test_stock_only_universe_keeps_its_etf_benchmark_and_skips_the_etf():
    # RES-01b: the strategy universe is preregistered as stocks only so adding
    # the benchmark ETF to the input cannot change what the strategy buys.
    data, p, spec = etf_market()
    stock_only = replace(
        p,
        logic=replace(
            p.logic,
            selection=replace(p.logic.selection, allowed_types=("STOCK",)),
        ),
    )
    reference = CommonEvaluator(stock_only).evaluate_benchmark(
        data, "market_etf", data.sessions[70], data.sessions[71]
    )
    assert {t["symbol"] for t in reference.trade_log} == {"FUND"}
    strategy = evaluate(data, stock_only, spec)
    assert "FUND" not in {t["symbol"] for t in strategy.trade_log}
    require_same_contract(strategy, reference)
