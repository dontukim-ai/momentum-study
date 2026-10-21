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
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
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
) -> AgentPolicy:
    p = AgentPolicy()
    logic = replace(
        p.logic,
        costs=replace(p.logic.costs, slippage_bps=HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS),
        selection=replace(p.logic.selection, spread_mode="historical_unavailable"),
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
    return replace(p, logic=logic)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--top-n", type=int)
    parser.add_argument("--champion-lookback", type=int)
    parser.add_argument("--lookbacks", help="comma-separated, e.g. 20,40,60")
    parser.add_argument("--max-drawdown", type=float)
    args = parser.parse_args()
    policy = historical_policy(
        top_n=args.top_n,
        champion_lookback=args.champion_lookback,
        lookbacks=tuple(int(n) for n in args.lookbacks.split(","))
        if args.lookbacks
        else None,
        max_drawdown=args.max_drawdown,
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
        f"max_drawdown={policy.logic.evaluation.max_drawdown}"
    )


if __name__ == "__main__":
    main()
