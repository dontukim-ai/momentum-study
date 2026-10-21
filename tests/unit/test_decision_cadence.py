"""Study-level decision cadence (LOOP-02b, 2026-10-21 user decision).

Explore run 3c rebalanced two stocks every session (turnover 21x in two
months). A study may now preregister weekly or monthly decisions: strategies
select and trade only on the first session of each ISO week or month, while
corporate actions, marks and write-offs still run daily.
"""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest
from test_eod_observation import historical, historical_policy
from trading_agent.contracts import AgentError, canonical

from momentum.agent.contracts import AgentPolicy, is_decision_session, policy_from_dict
from momentum.agent.evaluator import CommonEvaluator


def test_decision_sessions_follow_the_calendar():
    monday, tuesday = date(2024, 6, 3), date(2024, 6, 4)
    friday = date(2024, 5, 31)
    assert is_decision_session("daily", tuesday, monday)
    assert is_decision_session("weekly", monday, friday)
    assert not is_decision_session("weekly", tuesday, monday)
    assert is_decision_session("monthly", monday, friday)  # May -> June
    assert not is_decision_session("monthly", tuesday, monday)
    # The first evaluated session always decides.
    assert is_decision_session("monthly", tuesday, None)


def test_policy_registers_cadence_and_reads_old_policies_as_daily():
    with pytest.raises(AgentError, match="decision cadence"):
        AgentPolicy(decision_cadence="yearly")
    raw = json.loads(canonical(AgentPolicy(decision_cadence="weekly")))
    assert policy_from_dict(raw).decision_cadence == "weekly"
    del raw["decision_cadence"]
    assert policy_from_dict(raw).decision_cadence == "daily"


def evaluate(cadence):
    data = historical()
    p = replace(historical_policy(), decision_cadence=cadence)
    spec = replace(p.logic.champion, min_momentum=D("-.1"))
    return data, CommonEvaluator(p).evaluate(
        data, spec, data.sessions[100], data.sessions[160]
    )


@pytest.mark.parametrize("cadence", ["weekly", "monthly"])
def test_strategy_trades_only_on_decision_sessions(cadence):
    data, result = evaluate(cadence)
    evaluated = [d for d in data.sessions if data.sessions[100] <= d <= data.sessions[160]]
    allowed = {
        d.isoformat()
        for k, d in enumerate(evaluated)
        if is_decision_session(cadence, d, evaluated[k - 1] if k else None)
    }
    planned = {e["session"] for e in result.execution_events}
    assert result.trade_log
    assert planned <= allowed
    assert {t["session"] for t in result.trade_log} <= allowed
    # Holdings are still valued every session.
    assert len(result.equity_curve) == len(evaluated)
    assert result.evaluation_contract["decision_cadence"] == cadence


def test_cadence_changes_the_evaluation_contract_and_reduces_trading():
    _, daily = evaluate("daily")
    _, weekly = evaluate("weekly")
    assert daily.evaluation_contract["decision_cadence"] == "daily"
    assert daily.evaluation_contract_id != weekly.evaluation_contract_id
    assert len({e["session"] for e in weekly.execution_events}) < len(
        {e["session"] for e in daily.execution_events}
    )
