"""Comparison contracts describe the experiment, never the contender."""

from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_agent_tradability import capacity_market, evaluate
from trading_agent.contracts import AgentError, digest


def test_candidate_parameters_do_not_change_the_comparison_contract():
    from momentum.agent.evaluator import require_same_contract

    data, p, spec = capacity_market()
    a = evaluate(data, p, spec)
    b = evaluate(data, p, replace(spec, lookback=5, ranking="risk_adjusted"))
    require_same_contract(a, b)
    assert a.strategy_version != b.strategy_version
    assert a.evaluation_contract_id == b.evaluation_contract_id
    assert digest(a.evaluation_contract) == a.evaluation_contract_id


@pytest.mark.parametrize(
    "change,field",
    [
        ("window", "evaluation_window"),
        ("capital", "capital"),
        ("risk", "risk_limits"),
        ("stress", "cost_scenario"),
    ],
)
def test_different_economic_conditions_cannot_be_compared(change, field):
    from momentum.agent.evaluator import CommonEvaluator, require_same_contract

    data, p, spec = capacity_market()
    spec = replace(spec, gross_weight=D(".6"))
    p = replace(p, logic=replace(p.logic, champion=spec))
    a = evaluate(data, p, spec)
    other = p
    if change in {"capital", "risk"}:
        updates = (
            {"capital_limit": D(500000)}
            if change == "capital"
            else {"max_position": D(".3")}
        )
        other = replace(
            p, logic=replace(p.logic, selection=replace(p.logic.selection, **updates))
        )
    b = CommonEvaluator(other).evaluate(
        data,
        spec,
        data.sessions[69 if change == "window" else 70],
        data.sessions[71],
        stress=change == "stress",
    )
    assert (
        a.evaluation_contract["data_bundle_id"]
        == b.evaluation_contract["data_bundle_id"]
    )
    assert a.evaluation_contract_id != b.evaluation_contract_id
    with pytest.raises(
        AgentError, match="Results are not under the same evaluation contract"
    ) as err:
        require_same_contract(a, b)
    assert field in str(err.value)


def test_same_stress_contract_is_shared_by_distinct_candidates():
    from momentum.agent.evaluator import CommonEvaluator, require_same_contract

    data, p, spec = capacity_market()
    evaluator = CommonEvaluator(p)
    a = evaluator.evaluate(
        data, spec, data.sessions[70], data.sessions[71], stress=True
    )
    b = evaluator.evaluate(
        data,
        replace(spec, lookback=5),
        data.sessions[70],
        data.sessions[71],
        stress=True,
    )
    require_same_contract(a, b)
    assert (
        a.evaluation_contract["cost_scenario"]["name"]
        == "stress_2x_commission_slippage"
    )


def test_contract_ids_are_stored_for_each_comparison(tmp_path):
    import json

    from test_agent import market, policy
    from trading_agent.store import Store

    from momentum.agent.service import ResearchAgent

    store = Store(tmp_path / "contracts.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init("contracts", policy())
        agent.tick("contracts", market())
        e = json.loads(
            store.db.execute("SELECT evidence_json FROM agent_cycles").fetchone()[0]
        )
        assert len(e["development"]["evaluation_contract_ids"]) == 3
        for trial in e["trials"]:
            if trial["status"] == "COMPLETE":
                assert [f["evaluation_contract_id"] for f in trial["folds"]] == e[
                    "development"
                ]["evaluation_contract_ids"]
        h = e["holdout"]
        assert (
            h["evaluation_contract_id"]
            == h["candidate"]["evaluation_contract_id"]
            == h["baseline"]["evaluation_contract_id"]
        )
        assert (
            h["stress_evaluation_contract_id"]
            == h["stressed_candidate"]["evaluation_contract_id"]
        )
        assert h["evaluation_contract_id"] != h["stress_evaluation_contract_id"]
    finally:
        store.close()


def test_final_contract_mismatch_aborts_without_promoting_or_releasing_claim(
    tmp_path, monkeypatch
):
    from test_agent import market, policy
    from trading_agent.store import Store

    from momentum.agent.evaluator import CommonEvaluator
    from momentum.agent.service import ResearchAgent

    data = market()
    p = policy()
    original = CommonEvaluator.evaluate

    def mismatched(self, inputs, spec, start, end, **kwargs):
        result = original(self, inputs, spec, start, end, **kwargs)
        if start == data.sessions[186] and spec.version != p.logic.champion.version:
            contract = {**result.evaluation_contract, "valuation_basis": "different"}
            return replace(
                result,
                evaluation_contract=contract,
                evaluation_contract_id=digest(contract),
            )
        return result

    monkeypatch.setattr(CommonEvaluator, "evaluate", mismatched)
    store = Store(tmp_path / "mismatch.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init("mismatch", p)
        with pytest.raises(
            AgentError, match="same evaluation contract.*valuation_basis"
        ):
            agent.tick("mismatch", data)
        assert agent.status("mismatch")["revision"] == 0
        assert (
            store.db.execute(
                "SELECT count(*) FROM agent_intervals WHERE kind='holdout'"
            ).fetchone()[0]
            == 1
        )
    finally:
        store.close()
