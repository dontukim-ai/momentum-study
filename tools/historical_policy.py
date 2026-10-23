"""Write the default research policy preregistered for historical supplier data.

Historical FSC quotes carry no bid/ask, so a study on them must be initialized
with ``spread_mode=historical_unavailable`` and at least the preregistered
25 bps one-way slippage (data-sources §4). Everything else is the default
policy; nothing here is tuned on results.

    uv run python tools/historical_policy.py --out artifacts/<run>/policy.json

Explore v2 (docs/blueprint/explore-candidates-v2.md, decided 2026-10-21)
fixes the champion's holding count and the grid before results:

    uv run python tools/historical_policy.py --out ... --top-n 10 \
        --champion-lookback 20 --lookbacks 20,40,60 --max-drawdown 0.25

RES-01b adds a preregistered market ETF benchmark to a stock-only universe:

    ... --market-etf 069500 --allowed-types STOCK

Explore v3 (explore-candidates-v3.md) narrows the universe by liquidity:

    ... --top-n 5 --min-average-turnover 10000000000 \
        --capital 100000000 --max-order-notional 40000000
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from trading_agent.contracts import canonical

from momentum.agent.contracts import (
    HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS,
    AgentPolicy,
    policy_from_dict,
)


def historical_policy(
    *,
    top_n: int | None = None,
    champion_lookback: int | None = None,
    lookbacks: tuple[int, ...] | None = None,
    max_drawdown: float | None = None,
    decision_cadence: str = "daily",
    market_etf_symbol: str | None = None,
    allowed_types: tuple[str, ...] | None = None,
    min_average_turnover: str | None = None,
    capital: str | None = None,
    max_order_notional: str | None = None,
) -> AgentPolicy:
    p = AgentPolicy()
    logic = replace(
        p.logic,
        costs=replace(p.logic.costs, slippage_bps=HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS),
        selection=replace(
            p.logic.selection,
            spread_mode="historical_unavailable",
            allowed_types=allowed_types or p.logic.selection.allowed_types,
            min_average_turnover=Decimal(min_average_turnover)
            if min_average_turnover is not None
            else p.logic.selection.min_average_turnover,
            capital_limit=Decimal(capital)
            if capital is not None
            else p.logic.selection.capital_limit,
            max_order_notional=Decimal(max_order_notional)
            if max_order_notional is not None
            else p.logic.selection.max_order_notional,
        ),
    )
    champion = logic.champion
    if top_n is not None:
        champion = replace(champion, top_n=top_n)
    if champion_lookback is not None:
        champion = replace(champion, lookback=champion_lookback)
    # Each change goes through the dataclass constructors, so allocation and
    # search-space limits are checked exactly as at study initialization.
    logic = replace(
        logic,
        champion=champion,
        lookbacks=lookbacks if lookbacks is not None else logic.lookbacks,
        evaluation=replace(logic.evaluation, max_drawdown=max_drawdown)
        if max_drawdown is not None
        else logic.evaluation,
    )
    return replace(
        p,
        logic=logic,
        decision_cadence=decision_cadence,
        market_etf_symbol=market_etf_symbol,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top-n", type=int)
    parser.add_argument("--champion-lookback", type=int)
    parser.add_argument("--lookbacks", help="comma-separated, e.g. 20,40,60")
    parser.add_argument("--max-drawdown", type=float)
    parser.add_argument(
        "--decision-cadence", choices=("daily", "weekly", "monthly"), default="daily"
    )
    parser.add_argument("--market-etf", help="preregistered benchmark ETF, e.g. 069500")
    parser.add_argument("--allowed-types", help="strategy universe, e.g. STOCK")
    parser.add_argument(
        "--min-average-turnover",
        help="20-session average close x volume floor in KRW, e.g. 10000000000",
    )
    parser.add_argument("--capital", help="research capital in KRW, at most 100000000")
    parser.add_argument("--max-order-notional", help="per-order cap in KRW")
    args = parser.parse_args()
    policy = historical_policy(
        top_n=args.top_n,
        champion_lookback=args.champion_lookback,
        lookbacks=tuple(int(n) for n in args.lookbacks.split(","))
        if args.lookbacks
        else None,
        max_drawdown=args.max_drawdown,
        decision_cadence=args.decision_cadence,
        market_etf_symbol=args.market_etf,
        allowed_types=tuple(args.allowed_types.split(","))
        if args.allowed_types
        else None,
        min_average_turnover=args.min_average_turnover,
        capital=args.capital,
        max_order_notional=args.max_order_notional,
    )
    raw = json.loads(canonical(policy))
    if policy_from_dict(raw) != policy:
        raise SystemExit("policy does not round-trip")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"policy: {args.out} spread_mode={policy.logic.selection.spread_mode} "
        f"slippage_bps={policy.logic.costs.slippage_bps} "
        f"champion={policy.logic.champion.ranking}/{policy.logic.champion.lookback}"
        f"/top{policy.logic.champion.top_n}/gross{policy.logic.champion.gross_weight} "
        f"lookbacks={list(policy.logic.lookbacks)} "
        f"max_drawdown={policy.logic.evaluation.max_drawdown} "
        f"decision_cadence={policy.decision_cadence} "
        f"market_etf={policy.market_etf_symbol} "
        f"allowed_types={list(policy.logic.selection.allowed_types)} "
        f"min_average_turnover={policy.logic.selection.min_average_turnover} "
        f"capital={policy.logic.selection.capital_limit} "
        f"max_order_notional={policy.logic.selection.max_order_notional}"
    )


if __name__ == "__main__":
    main()
