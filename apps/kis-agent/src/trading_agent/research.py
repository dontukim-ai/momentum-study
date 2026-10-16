"""Bounded proposals, chronological development folds and consumed holdout registry."""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Protocol

from .contracts import AgentError, Costs, D, StrategySpec, canonical, digest
from .data import Snapshot
from .evaluation import backtest
from .store import Store


class Proposer(Protocol):
    """Future LLM adapters return validated specifications, never executable code."""

    def propose(self, champion: StrategySpec) -> list[StrategySpec]: ...


@dataclass(frozen=True)
class BoundedLookbacks:
    lookbacks: tuple[int, ...] = (5, 10, 20)
    budget: int = 8

    def propose(self, champion: StrategySpec) -> list[StrategySpec]:
        if type(self.budget) is not int or not 1 <= self.budget <= 30:
            raise AgentError("Experiment budget must be 1..30")
        values = tuple(dict.fromkeys(self.lookbacks))
        if not values or len(values) > self.budget or any(type(v) is not int or not 2 <= v <= 60 for v in values):
            raise AgentError("Only 2..60 session integer lookbacks within the trial budget are accepted")
        return [replace(champion, lookback=v) for v in values]


def paired_block_interval(differences: list[float], block: int = 5, samples: int = 1000) -> tuple[float, float]:
    """Deterministic circular block bootstrap for an indicative mean-return interval."""
    if len(differences) < 20:
        raise AgentError("Too few holdout returns even for an indicative interval")
    rng = random.Random(0)
    count = len(differences)
    estimates = []
    for _ in range(samples):
        resampled = []
        while len(resampled) < count:
            start = rng.randrange(count)
            resampled.extend(differences[(start + j) % count] for j in range(block))
        estimates.append(statistics.mean(resampled[:count]))
    estimates.sort()
    return estimates[int(.025 * samples)], estimates[int(.975 * samples)]


def research(snapshot: Snapshot, champion: StrategySpec, costs: Costs, store: Store,
             proposer: Proposer = BoundedLookbacks(), initial_cash: Decimal = D("10000000"),
             holdout_fraction: float = .2, min_improvement: float = .001,
             max_drawdown: float = .2, *, evaluator=None, candidate_validator=None,
             evaluation_binding: dict | None = None, minimum_warmup: int = 0,
             min_fold_trades: int = 0, min_holdout_trades: int = 0, on_registered=None) -> dict:
    if not .1 <= holdout_fraction <= .4 or min_improvement < 0 or not 0 < max_drawdown < 1:
        raise AgentError("Invalid preregistered evaluation policy")
    validator = getattr(proposer, "validate_context", None)
    if validator is not None:
        validator(snapshot, champion, holdout_fraction)
    candidates = proposer.propose(champion)
    # An adapter may change lookback only. Risk, weights, costs and evaluation remain fixed.
    if not candidates or len(candidates) > 30:
        raise AgentError("Invalid candidate count")
    if evaluator is not None and (evaluation_binding is None or candidate_validator is None):
        raise AgentError("A custom evaluator requires a bound policy and explicit candidate validator")
    evaluate = evaluator or backtest
    for spec in candidates:
        valid = candidate_validator(spec, champion) if candidate_validator else replace(spec, lookback=champion.lookback) == champion
        if not valid or not 2 <= spec.lookback <= 60:
            raise AgentError("Proposal exceeds the allowed parameter-change boundary")
    all_specs = {s.version: s for s in [champion] + candidates}
    sessions = snapshot.sessions
    holdout_count = max(30, int(len(sessions) * holdout_fraction))
    boundary = len(sessions) - holdout_count
    warmup = max(max(s.lookback for s in all_specs.values()) + 1, minimum_warmup)
    if boundary - warmup < 45 or holdout_count < 30:
        raise AgentError("Need at least 45 development sessions after warmup and 30 holdout sessions")
    config = {"engine": "bounded-research-v1", "snapshot_id": snapshot.id,
              "champion": champion, "candidates": list(all_specs.values()), "costs": costs,
              "capital": initial_cash, "holdout_fraction": holdout_fraction,
              "min_improvement": min_improvement, "max_drawdown": max_drawdown,
              "bootstrap": {"block": 5, "samples": 1000, "seed": 0}}
    proposal_metadata = getattr(proposer, "metadata", None)
    if evaluation_binding is not None:
        config["evaluation_binding"] = evaluation_binding
        config["activity_requirements"] = {"min_fold_trades": min_fold_trades, "min_holdout_trades": min_holdout_trades}
        config["minimum_warmup"] = minimum_warmup
    if proposal_metadata is not None:
        config["proposal"] = proposal_metadata
    run_id = digest(config)
    # Persist the immutable evaluation contract before any holdout read. A
    # consumed interval without a finished report still retains its definition.
    with store.transaction():
        frozen = store.db.execute("SELECT manifest_json FROM evaluation_manifests WHERE run_id=?", (run_id,)).fetchone()
        if frozen and frozen[0] != canonical(config):
            raise AgentError("Frozen evaluation manifest changed")
        store.db.execute("INSERT OR IGNORE INTO evaluation_manifests VALUES(?,?)", (run_id, canonical(config)))
    if on_registered is not None:
        on_registered(run_id)
    prior = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (run_id,)).fetchone()
    if prior:
        report = json.loads(prior[0])
        report["cached_report"] = True
        return report
    holdout_start, holdout_end = sessions[boundary].isoformat(), sessions[-1].isoformat()
    store.guard_unified_interval(holdout_start, holdout_end, run_id)
    # Reject overlap even after CSV edits or universe changes involving any consumed symbol.
    for symbol in snapshot.symbols:
        used = store.db.execute("SELECT run_id FROM holdouts WHERE symbol=? AND NOT(end<? OR start>?)",
                                (symbol, holdout_start, holdout_end)).fetchone()
        if used:
            raise AgentError("Holdout overlaps a consumed evaluation; use new forward data, not a new database")
    fold_size = (boundary - warmup) // 3
    folds = [(sessions[warmup + i * fold_size], sessions[(warmup + (i + 1) * fold_size - 1) if i < 2 else boundary - 1])
             for i in range(3)]
    trials = []
    for spec in all_specs.values():
        try:
            results = [evaluate(snapshot, spec, costs, initial_cash, start, end) for start, end in folds]
            score = statistics.mean(statistics.mean(r.daily_returns) for r in results)
            trials.append({"spec": asdict(spec), "version": spec.version, "status": "COMPLETE",
                           "dev_mean_daily_return": score,
                           "worst_fold_drawdown": max(r.max_drawdown for r in results),
                           "folds": [{"start": r.start, "end": r.end, "net_return": r.net_return,
                                      "drawdown": r.max_drawdown, "trades": r.trades} for r in results]})
        except AgentError as exc:
            trials.append({"spec": asdict(spec), "version": spec.version, "status": "FAILED", "reason": str(exc)})
    base = next(t for t in trials if t["version"] == champion.version)
    if base["status"] != "COMPLETE":
        raise AgentError("Champion failed development evaluation")
    eligible = [t for t in trials if t["status"] == "COMPLETE" and t["worst_fold_drawdown"] <= max_drawdown
                and all(f["trades"] >= min_fold_trades for f in t["folds"])]
    best = max(eligible, key=lambda t: (t["dev_mean_daily_return"], t["version"])) if eligible else base
    report = {"run_id": run_id, "cached_report": False, "mode": "LOCAL_RESEARCH_SCREENING",
              "configuration": config, "champion_version": champion.version,
              "selected_version": champion.version, "trials": trials, "total_trials": len(trials),
              "folds": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in folds],
              "holdout": {"start": holdout_start, "end": holdout_end, "consumed": False},
              "verdict": "KEEP_CHAMPION", "live_promotion": False,
              "limitations": [("Only preregistered bounded logic proposals; no code self-modification or order deployment" if evaluation_binding else
                               "Only bounded lookback proposals; no code self-modification or automatic deployment"),
                              "Same-process evaluation is a logical boundary, not OS-level isolation",
                              "Daily-open proxy and indicative bootstrap do not prove tradable alpha"]}
    if best["version"] != champion.version and best["dev_mean_daily_return"] > base["dev_mean_daily_return"]:
        selected = all_specs[best["version"]]
        report["selected_version"] = selected.version
        # Claim before evaluation; a crash leaves the interval consumed (fail closed).
        with store.transaction():
            store.guard_unified_interval(holdout_start, holdout_end, run_id)
            for symbol in snapshot.symbols:
                used = store.db.execute("SELECT 1 FROM holdouts WHERE symbol=? AND NOT(end<? OR start>?)",
                                        (symbol, holdout_start, holdout_end)).fetchone()
                if used:
                    raise AgentError("Holdout was concurrently consumed")
            store.db.executemany("INSERT INTO holdouts VALUES(?,?,?,?)",
                                 [(s, holdout_start, holdout_end, run_id) for s in snapshot.symbols])
        start, end = sessions[boundary], sessions[-1]
        baseline = evaluate(snapshot, champion, costs, initial_cash, start, end)
        candidate = evaluate(snapshot, selected, costs, initial_cash, start, end)
        stressed = replace(costs, slippage_bps=costs.slippage_bps * 2, commission_bps=costs.commission_bps * 2)
        stressed_base = evaluate(snapshot, champion, stressed, initial_cash, start, end)
        stressed_candidate = evaluate(snapshot, selected, stressed, initial_cash, start, end)
        interval = paired_block_interval([a - b for a, b in zip(candidate.daily_returns, baseline.daily_returns)])
        improvement = candidate.net_return - baseline.net_return
        passed = (improvement >= min_improvement and interval[0] > 0
                  and candidate.max_drawdown <= max_drawdown
                  and stressed_candidate.net_return > stressed_base.net_return
                  and candidate.trades >= min_holdout_trades)
        report["holdout"].update({"consumed": True, "champion_net_return": baseline.net_return,
                                 "candidate_net_return": candidate.net_return, "net_improvement": improvement,
                                 "mean_daily_difference_95pct_interval": interval,
                                 "candidate_drawdown": candidate.max_drawdown,
                                 "cost_stress_improvement": stressed_candidate.net_return - stressed_base.net_return,
                                 "observations": len(candidate.daily_returns)})
        report["holdout"]["candidate_trades"] = candidate.trades
        report["verdict"] = "ELIGIBLE_FOR_PAPER_REVIEW" if passed else "KEEP_CHAMPION"
    with store.transaction():
        store.db.execute("INSERT INTO experiments VALUES(?,?)", (run_id, canonical(report)))
    return report
