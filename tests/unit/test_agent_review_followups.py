"""Raw NAV residual checks and investability of reporting-only benchmarks."""

import json
from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_agent_tradability import capacity_market, evaluate
from trading_agent.contracts import AgentError
from trading_agent.data import Snapshot
from trading_agent.selection import SecurityHistory
from trading_agent.store import Store

from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.service import ResearchAgent, promotion_gates
from momentum.ledger.ledger import Ledger


@pytest.mark.parametrize("error", ["0.00000001", "-0.00000001", "0.1", "-0.1", "0.49"])
def test_sub_won_nav_errors_cannot_hide_behind_rounding(monkeypatch, error):
    data, p, spec = capacity_market()
    original = Ledger.mark_to_market

    def corrupt(ledger, closes, day):
        original(ledger, closes, day)
        ledger._nav += D(error)

    monkeypatch.setattr(Ledger, "mark_to_market", corrupt)
    with pytest.raises(AgentError, match="NAV residual"):
        evaluate(data, p, spec)


def test_small_absolute_nav_residual_is_recorded_without_changing_economics(
    monkeypatch,
):
    data, p, spec = capacity_market()
    original = Ledger.mark_to_market

    def residual(ledger, closes, day):
        original(ledger, closes, day)
        if day == data.sessions[70]:
            ledger._nav += D("1e-9")

    monkeypatch.setattr(Ledger, "mark_to_market", residual)
    r = evaluate(data, p, spec)
    assert r.net_return == 0
    assert all(D(row["nav_residual"]) == D("1e-9") for row in r.equity_curve)
    assert D(r.portfolio_diagnostics["max_abs_nav_residual"]) == D("1e-9")
    assert D(r.evaluation_contract["nav_residual_absolute_tolerance"]) == D("1e-9")


def test_normal_repeated_buys_and_sells_pass_tight_raw_nav_check():
    from test_agent import market, policy

    data = market()
    r = CommonEvaluator(policy()).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[186], data.sessions[265]
    )
    assert {t["side"] for t in r.trade_log} == {"BUY", "SELL"}
    assert all(abs(D(row["nav_residual"])) <= D("1e-9") for row in r.equity_curve)


def test_many_eligible_stocks_report_effective_cash_and_budget_exclusions():
    data, p, _ = capacity_market(symbols=tuple(f"S{i:03d}" for i in range(100)))
    data = replace(
        data,
        snapshot=Snapshot(
            [
                replace(b, open=D(10000), high=D(10000), low=D(10000), close=D(10000))
                for b in data.snapshot.bars
            ]
        ),
        securities=SecurityHistory(
            [replace(s, bid=D(10000), ask=D(10000)) for s in data.securities.records]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    d = r.portfolio_diagnostics
    assert d["budget_candidate_observations"] == d["unaffordable_observations"] == 100
    assert d["unaffordable_rate"] == 1
    assert d["average_invested_weight"] == 0
    assert d["average_cash_weight"] == 1
    assert d["average_holding_count"] == d["last_holding_count"] == 0
    assert r.trades == 0


def test_budget_exclusion_ratio_counts_only_otherwise_eligible_candidates():
    data, p, _ = capacity_market(symbols=("A", "B", "C", "D"))
    data = replace(
        data,
        snapshot=Snapshot(
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
                replace(s, bid=D(300000), ask=D(300000))
                if s.symbol == "C"
                else replace(s, managed=True)
                if s.symbol == "D"
                else s
                for s in data.securities.records
            ]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    d = r.portfolio_diagnostics
    assert d["budget_candidate_observations"] == 3
    assert d["unaffordable_observations"] == 1
    assert d["unaffordable_rate"] == pytest.approx(1 / 3)
    assert d["average_invested_weight"] == 0.264
    assert d["average_cash_weight"] == 0.736
    assert d["average_holding_count"] == 1
    assert d["last_holding_count"] == 2


def test_no_budget_candidates_is_na_instead_of_zero_and_cash_is_explicit():
    data, p, _ = capacity_market()
    evaluator = CommonEvaluator(p)
    cash = evaluator.evaluate_benchmark(
        data, "cash", data.sessions[70], data.sessions[71]
    )
    assert cash.portfolio_diagnostics["unaffordable_rate"] is None
    assert cash.portfolio_diagnostics["average_cash_weight"] == 1
    assert cash.portfolio_diagnostics["average_invested_weight"] == 0
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, managed=True) for s in data.securities.records]
        ),
    )
    empty = evaluator.evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    assert empty.portfolio_diagnostics["unaffordable_rate"] is None


def test_stocks_unaffordable_at_maximum_budget_are_counted_once():
    data, p, _ = capacity_market(symbols=("A", "B", "C"))
    data = replace(
        data,
        snapshot=Snapshot(
            [
                replace(
                    b, open=D(500000), high=D(500000), low=D(500000), close=D(500000)
                )
                if b.symbol == "C"
                else b
                for b in data.snapshot.bars
            ]
        ),
        securities=SecurityHistory(
            [
                replace(s, bid=D(500000), ask=D(500000)) if s.symbol == "C" else s
                for s in data.securities.records
            ]
        ),
    )
    r = CommonEvaluator(p).evaluate_benchmark(
        data, "equal_weight_eligible", data.sessions[70], data.sessions[71]
    )
    assert r.portfolio_diagnostics["budget_candidate_observations"] == 3
    assert r.portfolio_diagnostics["unaffordable_observations"] == 1
    assert r.portfolio_diagnostics["unaffordable_rate"] == pytest.approx(1 / 3)


def test_portfolio_diagnostics_are_in_holdout_and_shadow_reports_only(tmp_path):
    from test_agent import market, policy

    store = Store(tmp_path / "review.sqlite3")
    try:
        agent = ResearchAgent(store)
        p = replace(policy(), holdout_sessions=40)
        agent.init("review", p)
        report = agent.tick("review", market(270, cycle=False))
        evidence = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        for summaries in (
            evidence["holdout"]["benchmarks"],
            report["shadow"]["benchmarks"],
        ):
            for summary in summaries.values():
                diagnostics = summary["portfolio_diagnostics"]
                assert "average_cash_weight" in diagnostics
                assert "unaffordable_rate" in diagnostics
                diagnostics["unaffordable_rate"] = 1
        assert promotion_gates(evidence, p) == evidence["gates"]
    finally:
        store.close()
