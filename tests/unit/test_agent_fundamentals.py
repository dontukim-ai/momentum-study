"""Fundamental rankings and the market-cap universe in the research runtime (explore v7)."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_agent import market, policy
from test_agent_tradability import capacity_market
from trading_agent.contracts import AgentError
from trading_agent.fundamentals import FinancialRecord, FundamentalHistory, ShareCount
from trading_agent.selection import SecurityHistory
from trading_agent.store import Store

from momentum.agent.cli import read_input
from momentum.agent.contracts import needs_fundamentals
from momentum.agent.evaluator import CommonEvaluator
from momentum.agent.market import KRX
from momentum.agent.service import ResearchAgent

SHARES = {"A": 100, "B": 300, "C": 200}
EQUITY = {"A": 4000000, "B": 400000, "C": 600000}


def fundamentals_for(data):
    first, last = data.sessions[0], data.sessions[-1]
    filed = KRX.observed_at(first)
    financials = [
        FinancialRecord(s, first - timedelta(days=100), filed, "CFS", EQUITY[s], 10000000, 100000, "TEST")
        for s in SHARES
    ]
    runs = [ShareCount(s, first, last, filed, n, "TEST") for s, n in SHARES.items()]
    return FundamentalHistory(financials, runs)


def universe_market(cap_rank=2):
    data, p, spec = capacity_market(symbols=(*SHARES, "FUND"))
    data = replace(
        data,
        securities=SecurityHistory(
            [replace(s, asset_type="ETF") if s.symbol == "FUND" else s for s in data.securities.records]
        ),
        fundamentals=fundamentals_for(data),
    )
    p = replace(
        p,
        market_etf_symbol="FUND",
        logic=replace(p.logic, selection=replace(p.logic.selection, max_market_cap_rank=cap_rank)),
    )
    return data, p, replace(spec, top_n=1, gross_weight=D(".4"))


def test_value_ranking_trades_inside_the_market_cap_universe():
    data, p, spec = universe_market()
    start, end = data.sessions[70], data.sessions[71]
    value = CommonEvaluator(p).evaluate(data, replace(spec, ranking="value"), start, end)
    # Universe = B, C (largest caps; flat prices). A has the highest B/M but is outside it.
    assert {t["symbol"] for t in value.trade_log} == {"C"}
    everyone = replace(p, logic=replace(p.logic, selection=replace(p.logic.selection, max_market_cap_rank=None)))
    assert {t["symbol"] for t in CommonEvaluator(everyone).evaluate(data, replace(spec, ranking="value"), start, end).trade_log} == {"A"}
    momentum = CommonEvaluator(p).evaluate(data, replace(spec, top_n=2, gross_weight=D(".8")), start, end)
    assert {t["symbol"] for t in momentum.trade_log} == {"B", "C"}


def test_benchmarks_use_the_same_universe_and_keep_the_market_etf():
    data, p, _ = universe_market()
    evaluator = CommonEvaluator(p)
    start, end = data.sessions[70], data.sessions[71]
    assert {t["symbol"] for t in evaluator.evaluate_benchmark(data, "market_etf", start, end).trade_log} == {"FUND"}
    equal = evaluator.evaluate_benchmark(data, "equal_weight_eligible", start, end)
    assert {t["symbol"] for t in equal.trade_log} == {"B", "C", "FUND"}


def test_prefix_cuts_share_runs_and_fingerprint_binds_fundamentals():
    data, _, _ = universe_market()
    end = data.sessions[30]
    prefix = data.prefix(end)
    assert max(r.through for r in prefix.fundamentals.share_records) == end
    plain = replace(data, fundamentals=None)
    assert plain.prefix(end).fundamentals is None
    assert plain.fingerprint(end) != data.fingerprint(end)
    # Extending the runs past ``end`` (later data arriving) leaves the prefix fingerprint alone.
    longer = replace(
        data,
        fundamentals=FundamentalHistory(
            list(data.fundamentals.financial_records),
            [replace(r, through=r.through + timedelta(days=30)) for r in data.fundamentals.share_records],
        ),
    )
    assert longer.fingerprint(end) == data.fingerprint(end)


def test_policy_needing_fundamentals_is_refused_before_any_budget(tmp_path):
    p = policy()
    assert not needs_fundamentals(p)
    ranked = replace(p, logic=replace(p.logic, rankings=(*p.logic.rankings, "value"), candidate_budget=12))
    universe = replace(p, logic=replace(p.logic, selection=replace(p.logic.selection, max_market_cap_rank=200)))
    assert needs_fundamentals(ranked) and needs_fundamentals(universe)
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("v7", universe)
    with pytest.raises(AgentError, match="--financials and --shares"):
        agent.tick("v7", market(460))
    assert store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM agent_cycles").fetchone()[0] == 0
    store.close()


def test_read_input_loads_fundamentals_only_as_a_pair(tmp_path):
    (tmp_path / "prices.csv").write_text(
        "session,symbol,open,high,low,close,volume,available_at,source\n"
        "2024-01-02,A,10,10,10,10,100,2024-01-02T06:40:00+00:00,T\n",
        encoding="utf-8",
    )
    (tmp_path / "securities.jsonl").write_text(
        json.dumps({"symbol": "A", "effective_at": "2024-01-02T06:30:00+00:00",
                    "available_at": "2024-01-02T06:40:00+00:00", "status": "NORMAL", "asset_type": "STOCK",
                    "market": "KOSPI", "managed": False, "caution": False, "leveraged": False,
                    "inverse": False, "bid": None, "ask": None, "source": "T"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "financials.jsonl").write_text(
        '{"symbol":"A","period_end":"2023-12-31","available_at":"2024-01-02T06:40:00+00:00",'
        '"statement":"CFS","equity":"5","assets":"9","operating_income":"1","source":"T"}\n',
        encoding="utf-8",
    )
    (tmp_path / "shares.csv").write_text(
        "symbol,session,through,available_at,listed_shares,source\n"
        "A,2024-01-02,2024-01-02,2024-01-02T06:40:00+00:00,7,T\n",
        encoding="utf-8",
    )
    files = (tmp_path / "prices.csv", tmp_path / "securities.jsonl")
    data = read_input(*files, financials=tmp_path / "financials.jsonl", shares=tmp_path / "shares.csv")
    assert data.fundamentals.share_records[0].listed_shares == 7
    assert read_input(*files).fundamentals is None
    with pytest.raises(AgentError, match="together"):
        read_input(*files, financials=tmp_path / "financials.jsonl")
