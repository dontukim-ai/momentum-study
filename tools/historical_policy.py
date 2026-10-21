"""Write the default research policy preregistered for historical supplier data.

Historical FSC quotes carry no bid/ask, so a study on them must be initialized
with ``spread_mode=historical_unavailable`` and at least the preregistered
25 bps one-way slippage (data-sources §4). Everything else is the default
policy; nothing here is tuned on results.

    uv run python tools/historical_policy.py --out artifacts/<run>/policy.json
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


def historical_policy() -> AgentPolicy:
    p = AgentPolicy()
    logic = replace(
        p.logic,
        costs=replace(p.logic.costs, slippage_bps=HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS),
        selection=replace(p.logic.selection, spread_mode="historical_unavailable"),
    )
    return replace(p, logic=logic)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    policy = historical_policy()
    raw = json.loads(canonical(policy))
    if policy_from_dict(raw) != policy:
        raise SystemExit("policy does not round-trip")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"policy: {args.out} spread_mode={policy.logic.selection.spread_mode} "
        f"slippage_bps={policy.logic.costs.slippage_bps}"
    )


if __name__ == "__main__":
    main()
