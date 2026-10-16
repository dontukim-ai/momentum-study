"""Benchmarks share screening, execution and accounting without voting on gates."""

import json
from dataclasses import replace
from decimal import Decimal as D

from test_agent_tradability import capacity_market, evaluate
from trading_agent.selection import SecurityHistory
from trading_agent.store import Store

from momentum.agent.evaluator import CommonEvaluator, require_same_contract
from momentum.agent.service import ResearchAgent, promotion_gates


def test_cash_benchmark_uses_same_contract_and_constant_nav():
    data, p, spec = capacity_market()
    evaluator = CommonEvaluator(p)
    cash = evaluator.evaluate_benchmark(
        data, "cash", data.sessions[70], data.sessions[71]
    )
    require_same_contract(evaluate(data, p, spec), cash)
    assert cash.net_return == cash.max_drawdown == 0
    assert cash.trades == 0
    assert cash.daily_returns == (0.0, 0.0)
    assert all(D(r["equity"]) == D(1000000) for r in cash.equity_curve)


def test_equal_weight_all_eligible_ignores_momentum_rank_and_threshold():
    data, p, _ = capacity_market(symbols=("A", "B"))
    # Zero momentum fails the ordinary strategy's strict positive threshold.
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    assert {t["symbol"]: t["quantity"] for t in r.trade_log} == {"A": 100, "B": 100}
    assert r.equity_curve[-1]["positions"] == {"A": 100, "B": 100}
    assert r.equity_curve[-1]["cash"] == "200000"


def test_benchmark_does_not_redistribute_rejected_slots():
    data, p, _ = capacity_market(symbols=("A", "B", "C"))
    last = data.sessions[71]
    # C passes the initial maximum position budget but cannot buy one share of
    # its equal-weight slot. A/B must not absorb C's unused allocation.
    data = replace(
        data,
        snapshot=type(data.snapshot)(
            [
                replace(
                    b, open=D(300000), high=D(300000), low=D(300000), close=D(300000)
                )
                if b.symbol == "C"
                else b
                for b in data.snapshot.bars
            ]
        ),
        securities=SecurityHistory(
            [
                replace(s, bid=D(300000), ask=D(300000)) if s.symbol == "C" else s
                for s in data.securities.records
            ]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], last
    )
    assert {t["symbol"]: t["quantity"] for t in r.trade_log} == {"A": 66, "B": 66}


def test_no_eligible_benchmark_stays_in_cash():
    data, p, _ = capacity_market()
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, managed=True) for s in data.securities.records]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    assert r.trades == 0
    assert r.net_return == 0


def test_rebalanced_benchmark_nav_is_exact_at_declared_money_precision():
    from test_agent import market, policy

    data = market()
    r = CommonEvaluator(policy()).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[186], data.sessions[225]
    )
    for row in r.equity_curve:
        assert D(row["equity"]) == D(row["cash"]) + D(row["positions_value"]) + D(
            row["receivable"]
        )
        assert D(row["equity"]) == D(row["equity"]).quantize(D(1))


def test_benchmarks_are_reported_for_kept_cycle_and_shadow_without_changing_gates(
    tmp_path,
):
    from test_agent import market, policy

    data = market(270, cycle=False)
    store = Store(tmp_path / "benchmarks.sqlite3")
    try:
        p = replace(policy(), holdout_sessions=40)
        agent = ResearchAgent(store)
        agent.init("benchmarks", p)
        report = agent.tick("benchmarks", data)
        evidence = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        h = evidence["holdout"]
        assert h["evaluated"] is False
        assert set(h["benchmarks"]) == {"cash", "equal_weight_eligible"}
        assert all(
            b["comparison_strategy"] == "champion" for b in h["benchmarks"].values()
        )
        assert evidence["gates"] == {"independent_evaluation": False}
        for b in h["benchmarks"].values():
            b["net_return"] = 100.0
        assert promotion_gates(evidence, p) == evidence["gates"]
        assert set(report["shadow"]["benchmarks"]) == {"cash", "equal_weight_eligible"}
    finally:
        store.close()


def test_candidate_benchmark_advantages_are_reporting_only(tmp_path):
    from test_agent import market, policy

    store = Store(tmp_path / "candidate-benchmarks.sqlite3")
    try:
        p = policy()
        agent = ResearchAgent(store)
        agent.init("candidate", p)
        report = agent.tick("candidate", market())
        assert report["cycles"][0]["research_champion_updated"]
        evidence = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        h = evidence["holdout"]
        for b in h["benchmarks"].values():
            assert b["comparison_strategy"] == "candidate"
            assert b["contract_id"] == h["evaluation_contract_id"]
            assert b["net_advantage"] == h["candidate_return"] - b["net_return"]
            b["net_return"] = 1000.0
        assert promotion_gates(evidence, p) == evidence["gates"]
    finally:
        store.close()
