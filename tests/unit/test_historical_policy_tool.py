"""tools/historical_policy.py: signal rankings are opt-in (2026-10-25)."""

import importlib.util
import json
from pathlib import Path

import pytest
from trading_agent.contracts import AgentError, canonical
from trading_agent.selection import DEFAULT_RANKINGS

spec = importlib.util.spec_from_file_location(
    "historical_policy_tool",
    Path(__file__).resolve().parents[2] / "tools/historical_policy.py",
)
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tool)

V4 = {
    "top_n": 10,
    "champion_lookback": 20,
    "lookbacks": (20, 40, 60),
    "max_drawdown": 0.25,
    "decision_cadence": "monthly",
    "market_etf_symbol": "069500",
    "allowed_types": ("STOCK",),
    "min_average_turnover": "10000000000",
    "capital": "100000000",
    "max_order_notional": "40000000",
}


def test_without_new_flags_the_policy_is_unchanged():
    p = tool.historical_policy(**V4)
    assert p.logic.rankings == DEFAULT_RANKINGS
    assert p.logic.candidate_budget == 8


def test_signal_rankings_change_only_the_search_space():
    rankings = ("momentum", "short_reversal", "low_volatility", "momentum_ex_max")
    base = json.loads(canonical(tool.historical_policy(**V4)))
    signal = json.loads(
        canonical(
            tool.historical_policy(**V4, rankings=rankings, candidate_budget=11)
        )
    )
    assert signal["logic"]["rankings"] == list(rankings)
    assert signal["logic"]["candidate_budget"] == 11
    for raw in (base, signal):
        del raw["logic"]["rankings"], raw["logic"]["candidate_budget"]
    assert signal == base


def test_a_grid_larger_than_the_budget_is_refused():
    with pytest.raises(AgentError, match="challenger budget"):
        tool.historical_policy(
            **V4,
            rankings=("momentum", "short_reversal", "low_volatility", "momentum_ex_max"),
        )
