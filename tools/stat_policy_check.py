"""Offline Monte Carlo diagnostics; never chooses a policy or opens a holdout."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from time import perf_counter
from typing import Any

import numpy as np
from trading_agent.contracts import AgentError

from momentum.agent.contracts import AgentPolicy
from momentum.agent.service import confidence_interval


def ar1(rng: np.random.Generator, count: int, rho: float) -> list[float]:
    # Stationary initial variance avoids a transient low-volatility prefix.
    values = np.empty(count)
    values[0] = rng.normal(0, 0.01 / math.sqrt(1 - rho * rho))
    innovations = rng.normal(0, 0.01, count - 1)
    for i, shock in enumerate(innovations, 1):
        values[i] = rho * values[i - 1] + shock
    return values.tolist()


def integers(value: str) -> list[int]:
    return [int(part) for part in value.split(",")]


def check(args: argparse.Namespace) -> dict[str, Any]:
    begin = perf_counter()
    if (
        args.null_trials < 1
        or not math.isfinite(args.rho)
        or abs(args.rho) >= 1
        or not args.seeds
        or any(s < 0 for s in args.seeds)
        or len(set(args.seeds)) != len(args.seeds)
    ):
        raise AgentError(
            "Positive trial budget, unique nonnegative seeds and stationary rho required"
        )
    days = []
    day = args.development_start
    while day <= args.development_end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    if len(days) < 30:
        raise AgentError("Declare at least 30 synthetic development weekdays")
    base = AgentPolicy(
        holdout_sessions=len(days), bootstrap_samples=args.bootstrap_samples
    )
    policies = {v: replace(base, statistical_policy_version=v) for v in ("v1", "v2")}
    # Validate every requested block before starting simulations.
    for block in args.blocks:
        replace(
            base,
            bootstrap_block=block,
            embargo_sessions=max(base.embargo_sessions, block),
        )
    if not args.blocks or not args.stability_samples:
        raise AgentError("Explicit block and resampling sensitivity lists required")
    rng = np.random.default_rng(args.seed)
    null = {v: 0 for v in policies}
    for _ in range(args.null_trials):
        values = ar1(rng, len(days), args.rho)
        for v, policy in policies.items():
            null[v] += confidence_interval(values, policy)[0] > 0
    null_policies = {}
    for v, hits in null.items():
        rate = hits / args.null_trials
        policy = policies[v]
        null_policies[v] = {
            "lower_positive_count": hits,
            "lower_positive_rate": rate,
            "trials": args.null_trials,
            "monte_carlo_standard_error": math.sqrt(
                rate * (1 - rate) / args.null_trials
            ),
            "nominal_one_sided_alpha_per_cycle": policy.family_alpha
            / policy.max_validation_cycles
            / (2 if v == "v1" else 1),
            "bootstrap_samples": policy.bootstrap_samples,
        }
    development = ar1(rng, len(days), args.rho)
    stability: dict[str, Any] = {}
    sensitivity: dict[str, Any] = {
        "input_role": "development",
        "rule": "freeze block on development before independent validation",
    }
    for v, policy in policies.items():
        stability[v] = []
        for samples in args.stability_samples:
            try:
                trial_policy = replace(policy, bootstrap_samples=samples)
            except AgentError as exc:
                stability[v].append(
                    {
                        "bootstrap_samples": samples,
                        "status": "policy_rejected",
                        "reason": str(exc),
                    }
                )
                continue
            lowers = [
                confidence_interval(development, trial_policy, seed=seed)[0]
                for seed in args.seeds
            ]
            stability[v].append(
                {
                    "bootstrap_samples": samples,
                    "status": "evaluated",
                    "seeds": args.seeds,
                    "lower_bounds": lowers,
                    "minimum": min(lowers),
                    "maximum": max(lowers),
                    "standard_deviation": float(np.std(lowers)),
                }
            )
        sensitivity[v] = [
            {
                "block": block,
                "lower": confidence_interval(
                    development,
                    replace(
                        policy,
                        bootstrap_block=block,
                        embargo_sessions=max(policy.embargo_sessions, block),
                    ),
                )[0],
            }
            for block in args.blocks
        ]
    return {
        "mode": "SYNTHETIC_STATISTICAL_DIAGNOSTICS",
        "development_window": {
            "start": args.development_start.isoformat(),
            "end": args.development_end.isoformat(),
            "observations": len(days),
            "calendar": "synthetic weekdays; not an exchange calendar",
        },
        "seed": args.seed,
        "null_simulation": {
            "process": "zero-population-mean AR(1)",
            "rho": args.rho,
            "endpoint": "single-cycle lower > 0 only; not combined promotion or lifetime family error",
            "policies": null_policies,
        },
        "quantile_stability": stability,
        "block_sensitivity": sensitivity,
        "policy_modified": False,
        "market_error_rate_guaranteed": False,
        "elapsed_seconds": perf_counter() - begin,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-start", type=date.fromisoformat, required=True)
    parser.add_argument("--development-end", type=date.fromisoformat, required=True)
    parser.add_argument("--null-trials", type=int, default=100)
    parser.add_argument("--bootstrap-samples", type=int, default=10000)
    parser.add_argument("--stability-samples", type=integers, default=[2000, 10000])
    parser.add_argument("--seeds", type=integers, default=[0, 1, 2, 3, 4])
    parser.add_argument("--blocks", type=integers, default=[2, 5, 10])
    parser.add_argument("--rho", type=float, default=0.4)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    try:
        result = check(args)
    except (AgentError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, allow_nan=False, sort_keys=True))


if __name__ == "__main__":
    main()
