"""Forward record (forward-record-v9): the fixed targets on sessions after the start.

Every target is evaluated from cash over [start, end] on one research input:

    uv run python tools/fwd_record.py --input data/krx/fwd9/<end>/research \
        --financials data/krx/fwd9/<end>/financials/financials.jsonl \
        --shares data/krx/fwd9/<end>/financials/shares.csv

All policies derive from config/forward-v9/base-policy.json (the v6 study
policy) exactly as the v7, C1, C3 and C4 studies changed it; their canonical
hashes are printed so a later month can show nothing moved. The §4 criteria
are printed for reference only: the judgement is due in 2027-10. Read-only:
nothing is written, no budget is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from trading_agent.contracts import canonical
from trading_agent.selection import RankedStrategy

from momentum.agent.cli import read_input
from momentum.agent.contracts import AgentPolicy, ResearchInput, policy_from_dict
from momentum.agent.evaluator import CommonEvaluator, EvaluationResult
from momentum.config import SpecUndefinedError

START = date(2026, 10, 21)
BASE = Path(__file__).resolve().parents[1] / "config/forward-v9/base-policy.json"
ETF, FULL, C4_NAMES = "069500", D("0.95"), 10


@dataclass(frozen=True)
class Target:
    name: str
    label: str
    policy: AgentPolicy
    spec: RankedStrategy | None  # None: the policy's market ETF benchmark


def policies(base: AgentPolicy) -> dict[str, AgentPolicy]:
    """The study policies, rebuilt from the v6 policy as each study changed it."""
    s = base.logic.selection
    if (
        base.market_etf_symbol != ETF
        or s.max_market_cap_rank is not None
        or s.market_symbol is not None
        or base.logic.rankings != ("momentum", "short_reversal", "low_volatility", "momentum_ex_max")
    ):
        raise SpecUndefinedError("base policy is not the v6 study policy")
    v7 = replace(base, logic=replace(
        base.logic,
        rankings=("momentum", "value", "profitability", "value_profit"),
        candidate_budget=5,
        selection=replace(s, max_market_cap_rank=200),
    ))
    capital = s.capital_limit
    return {
        "v6": base,
        "v7": v7,
        # explore v8 C1: the trend filter may hold the ETF.
        "c1": replace(base, logic=replace(base.logic, selection=replace(
            s, allowed_types=("STOCK", "ETF"), market_symbol=ETF))),
        # explore v8 C3: v7 with the market series for the residual.
        "c3": replace(v7, logic=replace(v7.logic, selection=replace(
            v7.logic.selection, market_symbol=ETF))),
        # explore v8 §7 C4: top 10 market caps, 9.5% each, monthly.
        "c4": replace(v7, decision_cadence="monthly", logic=replace(v7.logic, selection=replace(
            v7.logic.selection, max_market_cap_rank=C4_NAMES, max_gross=FULL, max_order_notional=capital))),
        "etf95": replace(v7, logic=replace(v7.logic, selection=replace(
            v7.logic.selection, max_position=FULL, max_gross=FULL, max_order_notional=capital))),
    }


def targets(base: AgentPolicy) -> list[Target]:
    p = policies(base)
    champion = p["v7"].logic.champion
    if (champion.ranking, champion.lookback, champion.top_n) != ("momentum", 20, 10):
        raise SpecUndefinedError("champion is not momentum/20/top10")
    return [
        Target("R1", "ETF 069500, 40% limit", p["v7"], None),
        Target("R2", "ETF 069500, 95%", p["etf95"], None),
        Target("S1", "momentum/20/top10, all stocks", p["v6"], p["v6"].logic.champion),
        Target("S2", "momentum/20/top10, top 200", p["v7"], champion),
        Target("S3", "value/top10, top 200", p["v7"], replace(champion, ranking="value")),
        Target("S4", "value_profit/top10, top 200", p["v7"], replace(champion, ranking="value_profit")),
        Target("S5", "C1 market_trend 069500 0.4", p["c1"], RankedStrategy(20, 1, D(".4"), ranking="market_trend")),
        Target("S6", "C3 residual_momentum/60/top10", p["c3"], replace(
            champion, ranking="residual_momentum", lookback=60)),
        Target("S7", "C4 market_cap_trend/top10 9.5%", p["c4"], replace(
            champion, ranking="market_cap_trend", top_n=C4_NAMES, gross_weight=FULL)),
    ]


def policy_hash(policy: AgentPolicy) -> str:
    return hashlib.sha256(canonical(policy).encode()).hexdigest()[:16]


def evaluate(target: Target, data: ResearchInput, start: date, end: date) -> EvaluationResult:
    evaluator = CommonEvaluator(target.policy)
    if target.spec is None:
        return evaluator.evaluate_benchmark(data, "market_etf", start, end)
    return evaluator.evaluate(data, target.spec, start, end)


def record(data: ResearchInput, base: AgentPolicy, start: date, end: date | None = None) -> dict:
    sessions = [d for d in data.sessions if d >= start]
    if end is None:
        end = data.sessions[-1]
    if not sessions or sessions[0] > end:
        raise SpecUndefinedError(f"no session from {start} to {end}")
    prefix = data.prefix(end)
    rows = {}
    for t in targets(base):
        r = evaluate(t, prefix, start, end)
        rows[t.name] = {
            "label": t.label,
            "policy": policy_hash(t.policy),
            "net_return": r.net_return,
            "max_drawdown": r.max_drawdown,
            "trades": r.trades,
            "average_invested_weight": r.portfolio_diagnostics.get("average_invested_weight"),
        }
    r2 = rows["R2"]
    for name, row in rows.items():
        if name.startswith("S"):
            # forward-record-v9 §4, reference only until the 2027-10 judgement.
            row["a_return_ge_r2"] = row["net_return"] >= r2["net_return"]
            row["b_mdd_le_1_5_r2"] = row["max_drawdown"] <= 1.5 * r2["max_drawdown"]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sum(1 for d in sessions if d <= end),
        "data_bundle_id": prefix.fingerprint(end),
        "targets": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--financials", type=Path, required=True)
    parser.add_argument("--shares", type=Path, required=True)
    parser.add_argument("--base-policy", type=Path, default=BASE)
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args(argv)
    root = args.input
    data = read_input(
        root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
        securities_mode="session-confirmed", financials=args.financials, shares=args.shares,
    )
    base = policy_from_dict(json.loads(args.base_policy.read_text(encoding="utf-8")))
    result = record(data, base, args.start, args.end)
    print(f"forward record {result['start']}~{result['end']} ({result['sessions']} sessions) "
          f"data {result['data_bundle_id']}")
    print(f"{'':<3} {'target':<32} {'net':>9} {'MDD':>7} {'trades':>6} {'invested':>8}  (a)   (b)   policy")
    for name, row in result["targets"].items():
        invested = row["average_invested_weight"]
        crit = (f"{row['a_return_ge_r2']!s:<5} {row['b_mdd_le_1_5_r2']!s:<5}" if "a_return_ge_r2" in row
                else f"{'-':<5} {'-':<5}")
        print(f"{name:<3} {row['label']:<32} {row['net_return']:>+9.2%} {row['max_drawdown']:>7.2%} "
              f"{row['trades']:>6} {'-' if invested is None else f'{invested:.2f}':>8}  {crit} {row['policy']}")
    print("(a) net >= R2, (b) MDD <= 1.5 x R2: reference only, judged in 2027-10 (forward-record-v9 §4)")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
