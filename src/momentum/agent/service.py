"""Persistent four-stage agent with reserved, non-overlapping forward windows."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from trading_agent.contracts import AgentError, canonical, digest, session_time
from trading_agent.logic import LogicProposer, ranked_spec
from trading_agent.selection import select_candidates
from trading_agent.store import Store

from .contracts import (
    AgentPolicy,
    ResearchInput,
    build_id,
    policy_from_dict,
    require_observed,
)
from .evaluator import (
    BENCHMARKS,
    CommonEvaluator,
    EvaluationResult,
    MarketCosts,
    require_same_contract,
)


def benchmark_report(
    evaluator: CommonEvaluator,
    data: ResearchInput,
    start: date,
    end: date,
    reference: EvaluationResult,
    compared_as: str,
) -> dict[str, Any]:
    results = {}
    for kind in BENCHMARKS:
        result = evaluator.evaluate_benchmark(data, kind, start, end)
        require_same_contract(reference, result)
        results[kind] = {
            "net_return": result.net_return,
            "max_drawdown": result.max_drawdown,
            "trades": result.trades,
            "contract_id": result.evaluation_contract_id,
            "net_advantage": reference.net_return - result.net_return,
            "comparison_strategy": compared_as,
            "comparison_strategy_version": reference.strategy_version,
            "portfolio_diagnostics": result.portfolio_diagnostics,
        }
    return results


FAMILY_LINEAGE_ID = "ranked-momentum"


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_budget_accounts (
 account_id TEXT PRIMARY KEY, market TEXT NOT NULL, family_lineage_id TEXT NOT NULL,
 total_cycles INTEGER NOT NULL, created_by TEXT NOT NULL,
 UNIQUE(market, family_lineage_id));
CREATE TABLE IF NOT EXISTS agent_budget_entries (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 account_id TEXT NOT NULL REFERENCES agent_budget_accounts,
 kind TEXT NOT NULL CHECK(kind IN ('reserve','consume')),
 cycle_id TEXT NOT NULL, study TEXT NOT NULL,
 UNIQUE(account_id, kind, cycle_id));
CREATE TABLE IF NOT EXISTS agent_studies (
 name TEXT PRIMARY KEY, policy_json TEXT NOT NULL, build TEXT NOT NULL,
 revision INTEGER NOT NULL, champion_json TEXT NOT NULL,
 checkpoint_end TEXT, checkpoint_hash TEXT);
CREATE TABLE IF NOT EXISTS agent_cycles (
 id TEXT PRIMARY KEY, study TEXT NOT NULL REFERENCES agent_studies(name), ordinal INTEGER NOT NULL,
 status TEXT NOT NULL, manifest_json TEXT NOT NULL, evidence_json TEXT, evidence_hash TEXT, result_json TEXT,
 UNIQUE(study,ordinal));
CREATE UNIQUE INDEX IF NOT EXISTS agent_one_pending ON agent_cycles(study)
 WHERE status IN ('REGISTERED','EVALUATED','FAILED');
CREATE TABLE IF NOT EXISTS agent_intervals (
 market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(market,owner,kind));
CREATE TABLE IF NOT EXISTS agent_shadow (
 cycle_id TEXT PRIMARY KEY REFERENCES agent_cycles(id), end TEXT NOT NULL,
 input_hash TEXT NOT NULL, report_json TEXT NOT NULL, report_hash TEXT NOT NULL);
"""


def confidence_interval(
    values: list[float], policy: AgentPolicy, *, seed: int = 0
) -> tuple[float, float | None]:
    """Paired circular blocks, Bonferroni budget across registered cycles.

    This is an indicative stationary-return test, not the original US H1-H4
    Holm procedure. One development-selected challenger reaches each holdout.
    """
    if len(values) < policy.holdout_sessions or not np.all(np.isfinite(values)):
        raise AgentError("Incomplete/nonfinite independent observations")
    n, block = len(values), policy.bootstrap_block
    rng = np.random.default_rng(seed)
    starts = rng.integers(
        0, n, size=(policy.bootstrap_samples, (n + block - 1) // block)
    )
    indices = ((starts[:, :, None] + np.arange(block)) % n).reshape(
        policy.bootstrap_samples, -1
    )[:, :n]
    means = np.sort(np.asarray(values)[indices].mean(axis=1))
    tail = policy.family_alpha / (2 * policy.max_validation_cycles)
    if policy.statistical_policy_version == "v2":
        tail = policy.family_alpha / policy.max_validation_cycles
        # Null is an unbounded upper endpoint, not a second estimated quantile.
        return float(means[int(tail * len(means))]), None
    return float(means[int(tail * len(means))]), float(
        means[min(len(means) - 1, int((1 - tail) * len(means)))]
    )


def promotion_gates(evidence: dict[str, Any], policy: AgentPolicy) -> dict[str, bool]:
    h, dev = evidence["holdout"], evidence["development"]
    if not h["evaluated"]:
        return {"independent_evaluation": False}
    interval = h["interval"]
    confidence = (
        len(interval) == 2 and 0 < interval[0] and interval[1] is None
        if policy.statistical_policy_version == "v2"
        else len(interval) == 2 and 0 < interval[0] <= interval[1]
    )
    return {
        "new_candidate": evidence["selected_version"] != evidence["champion_version"],
        "development_advantage": dev["candidate_score"] > dev["champion_score"],
        "development_risk": dev["worst_drawdown"]
        <= policy.logic.evaluation.max_drawdown,
        "development_activity": min(dev["fold_trades"])
        >= policy.logic.evaluation.min_fold_trades,
        "net_improvement": h["candidate_return"] - h["champion_return"]
        >= policy.logic.evaluation.min_improvement,
        "confidence": confidence,
        "drawdown": 0
        <= h["candidate_drawdown"]
        <= policy.logic.evaluation.max_drawdown,
        "cost_stress": h["stress_improvement"] > 0,
        "activity": h["candidate_trades"] >= policy.logic.evaluation.min_holdout_trades,
        "independent_observations": h["observations"] == policy.holdout_sessions,
    }


class ResearchAgent:
    def __init__(self, store: Store):
        self.store = store
        self.db = store.db
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('agent_schema_version','1')")
        if (
            self.db.execute(
                "SELECT value FROM meta WHERE key='agent_schema_version'"
            ).fetchone()[0]
            != "1"
        ):
            raise AgentError(
                "Unsupported unified research schema; preserve the database"
            )

    def import_history(self, source: Path) -> dict[str, Any]:
        """Import consumption only, without accounts, credentials or experiments.

        Overlapping historical claims are facts to preserve, not new evaluations
        to reject. A deterministic imported owner makes repeated imports safe.
        """
        if not source.is_file() or str(source.resolve()) == str(
            Path(self.store.path).resolve()
        ):
            raise AgentError("Import requires a separate existing research database")
        connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = {
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            claims: list[tuple[str, str, str, str, str]] = []
            if "holdouts" in tables:
                claims.extend(
                    ("KRX", r[1], r[2], "legacy:" + r[3], "holdout")
                    for r in connection.execute(
                        "SELECT symbol,start,end,run_id FROM holdouts"
                    )
                )
            if "agent_intervals" in tables:
                claims.extend(
                    tuple(r)
                    for r in connection.execute(
                        "SELECT market,start,end,owner,kind FROM agent_intervals"
                    )
                )
            if not {"holdouts", "agent_intervals"} & tables:
                raise AgentError(
                    "Source has no recognized evaluation consumption history"
                )
            with self.store.transaction():
                for market, start, end, owner, kind in set(claims):
                    if market != "KRX" or date.fromisoformat(
                        start
                    ) > date.fromisoformat(end):
                        raise AgentError(
                            "Unsupported market or malformed historical interval"
                        )
                    imported_owner = "import:" + digest(
                        [market, start, end, owner, kind]
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO agent_intervals VALUES(?,?,?,?,?)",
                        (market, start, end, imported_owner, kind),
                    )
                self.store.event(
                    "agent:history",
                    None,
                    "AGENT_CONSUMPTION_IMPORTED",
                    {
                        "claims": len(set(claims)),
                        "history_hash": digest(sorted(set(claims))),
                    },
                )
            return {
                "imported_distinct_claims": len(set(claims)),
                "history_hash": digest(sorted(set(claims))),
                "source_unchanged": True,
                "orders_submitted": 0,
            }
        finally:
            connection.close()

    def init(self, name: str, policy: AgentPolicy) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise AgentError(
                "Study name must use 1..80 ASCII letters, digits, underscores or hyphens"
            )
        with self.store.transaction():
            row = self.db.execute(
                "SELECT * FROM agent_studies WHERE name=?", (name,)
            ).fetchone()
            if row and (
                canonical(policy_from_dict(json.loads(row["policy_json"])))
                != canonical(policy)
                or row["build"] != build_id()
            ):
                raise AgentError(
                    "Preserve the existing study policy, build and consumed history"
                )
            self.db.execute(
                "INSERT OR IGNORE INTO agent_studies VALUES(?,?,?,?,?,?,?)",
                (
                    name,
                    canonical(policy),
                    build_id(),
                    0,
                    canonical(policy.logic.champion),
                    None,
                    None,
                ),
            )
        return self.status(name)

    def _budget_account_id(self, policy: AgentPolicy) -> str:
        return digest({"market": policy.market, "family_lineage_id": FAMILY_LINEAGE_ID})

    def _budget_account(self, policy: AgentPolicy, name: str) -> Any:
        account_id = self._budget_account_id(policy)
        self.db.execute(
            "INSERT OR IGNORE INTO agent_budget_accounts VALUES(?,?,?,?,?)",
            (
                account_id,
                policy.market,
                FAMILY_LINEAGE_ID,
                policy.max_validation_cycles,
                name,
            ),
        )
        return self.db.execute(
            "SELECT * FROM agent_budget_accounts WHERE account_id=?", (account_id,)
        ).fetchone()

    def _budget_entry(
        self, account_id: str, kind: str, cycle_id: str, name: str
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO agent_budget_entries(account_id,kind,cycle_id,study) VALUES(?,?,?,?)",
            (account_id, kind, cycle_id, name),
        )

    def _migrate_budget(self) -> None:
        # The first persisted registration owns the original allocation. Historical
        # overuse is retained in full rather than granting a fresh budget.
        if self.db.execute("SELECT 1 FROM agent_budget_accounts").fetchone():
            return
        for cycle in self.db.execute("SELECT * FROM agent_cycles ORDER BY rowid"):
            manifest = json.loads(cycle["manifest_json"])
            policy = policy_from_dict(manifest["policy"])
            account = self._budget_account(policy, cycle["study"])
            self._budget_entry(
                account["account_id"], "reserve", cycle["id"], cycle["study"]
            )
            if cycle["status"] not in {"REGISTERED", "EVALUATED"}:
                self._budget_entry(
                    account["account_id"], "consume", cycle["id"], cycle["study"]
                )

    def _budget(self, policy: AgentPolicy) -> dict[str, Any]:
        account_id = self._budget_account_id(policy)
        account = self.db.execute(
            "SELECT * FROM agent_budget_accounts WHERE account_id=?", (account_id,)
        ).fetchone()
        counts = dict(
            self.db.execute(
                "SELECT kind,count(*) FROM agent_budget_entries WHERE account_id=? GROUP BY kind",
                (account_id,),
            )
        )
        reserved, consumed = counts.get("reserve", 0), counts.get("consume", 0)
        total = account["total_cycles"] if account else policy.max_validation_cycles
        return {
            "account_id": account_id,
            "market": policy.market,
            "family_lineage_id": FAMILY_LINEAGE_ID,
            "total_cycles": total,
            "reserved": reserved,
            "consumed": consumed,
            "remaining": total - reserved,
        }

    def _consume_budget(self, cycle_id: str) -> None:
        self._migrate_budget()
        cycle = self.db.execute(
            "SELECT * FROM agent_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        policy = policy_from_dict(json.loads(cycle["manifest_json"])["policy"])
        self._budget_entry(
            self._budget_account_id(policy), "consume", cycle_id, cycle["study"]
        )

    def status(self, name: str) -> dict[str, Any]:
        row = self._study(name)
        with self.store.transaction():
            self._migrate_budget()
            budget = self._budget(policy_from_dict(json.loads(row["policy_json"])))
        return {
            "study": name,
            "budget": budget,
            "mode": "AUTOMATED_RESEARCH_ONLY",
            "revision": row["revision"],
            "champion": json.loads(row["champion_json"]),
            "build": row["build"],
            "policy": json.loads(row["policy_json"]),
            "checkpoint_end": row["checkpoint_end"],
            "cycles": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,ordinal,status FROM agent_cycles WHERE study=? ORDER BY ordinal",
                    (name,),
                )
            ],
            "capabilities": {
                "selection": "KIS-point-in-time",
                "accounting": "momentum.Ledger",
                "market": "KRX",
                "legacy_us_h1_h4_final_evaluation": False,
            },
            "orders_submitted": 0,
            "live_enabled": False,
        }

    def _study(self, name: str) -> Any:
        row = self.db.execute(
            "SELECT * FROM agent_studies WHERE name=?", (name,)
        ).fetchone()
        if not row:
            raise AgentError("Initialize the persistent agent study first")
        if row["build"] != build_id():
            raise AgentError(
                "Study source changed; preserve build and consumed history for migration"
            )
        return row

    def _checkpoint(self, name: str, data: ResearchInput, end: date) -> None:
        row = self._study(name)
        if row["checkpoint_end"]:
            prior = date.fromisoformat(row["checkpoint_end"])
            if (
                prior > data.sessions[-1]
                or data.fingerprint(prior) != row["checkpoint_hash"]
            ):
                raise AgentError(
                    "Input changed frozen price, security, action or session history"
                )
            if end <= prior:
                return
        self.db.execute(
            "UPDATE agent_studies SET checkpoint_end=?,checkpoint_hash=? WHERE name=?",
            (end.isoformat(), data.fingerprint(end), name),
        )

    def _claim(
        self,
        policy: AgentPolicy,
        start: date,
        end: date,
        owner: str,
        kind: str,
        data: ResearchInput,
    ) -> None:
        args = (policy.market, owner, start.isoformat(), end.isoformat())
        if self.db.execute(
            "SELECT 1 FROM agent_intervals WHERE market=? AND owner<>? AND NOT(end<? OR start>?)",
            args,
        ).fetchone():
            raise AgentError("Evaluation interval overlaps consumed market evidence")
        # Legacy KIS tables participate in the same transaction. This prevents
        # switching to the old CLI or renaming the universe to reopen evidence.
        if self.db.execute(
            "SELECT 1 FROM holdouts WHERE run_id<>? AND NOT(end<? OR start>?)", args[1:]
        ).fetchone():
            raise AgentError(
                "Evaluation interval overlaps consumed legacy KIS evidence"
            )
        self.db.execute(
            "INSERT INTO agent_intervals VALUES(?,?,?,?,?) ON CONFLICT(market,owner,kind) DO UPDATE SET end=MAX(end,excluded.end)",
            (policy.market, start.isoformat(), end.isoformat(), owner, kind),
        )
        self.db.executemany(
            "INSERT OR IGNORE INTO holdouts VALUES(?,?,?,?)",
            [
                (s, start.isoformat(), end.isoformat(), owner)
                for s in data.snapshot.symbols
            ],
        )

    def _plan(
        self, data: ResearchInput, policy: AgentPolicy, previous: Any
    ) -> dict[str, int]:
        if previous is None:
            dev_end = policy.warmup_sessions + policy.development_sessions - 1
        else:
            old = json.loads(previous["manifest_json"])
            dev_end = (
                data.sessions.index(date.fromisoformat(old["holdout_end"]))
                + policy.shadow_sessions
            )
        return {
            "dev_start": dev_end - policy.development_sessions + 1,
            "dev_end": dev_end,
            "holdout_start": dev_end + policy.embargo_sessions + 1,
            "holdout_end": dev_end + policy.embargo_sessions + policy.holdout_sessions,
        }

    def tick(
        self, name: str, data: ResearchInput, *, max_cycles: int = 1
    ) -> dict[str, Any]:
        require_observed(data)
        row = self._study(name)
        policy = policy_from_dict(json.loads(row["policy_json"]))
        if (
            type(max_cycles) is not int
            or not 1 <= max_cycles <= policy.max_validation_cycles
        ):
            raise AgentError("max_cycles must fit the fixed total validation budget")
        with self.store.transaction():
            self._checkpoint(name, data, data.sessions[0])
        completed: list[dict[str, Any]] = []
        previous = None
        for ordinal in range(max_cycles):
            prior = self.db.execute(
                "SELECT * FROM agent_cycles WHERE study=? AND ordinal=?",
                (name, ordinal),
            ).fetchone()
            if prior:
                if prior["status"] == "ABANDONED":
                    previous = prior
                    continue
                if prior["status"] != "COMPLETE":
                    raise AgentError(
                        "Cycle interrupted; recover stored evidence or explicitly abandon after stopping the worker"
                    )
                completed.append({**self.recover(prior["id"]), "cached": True})
                previous = prior
                continue
            plan = self._plan(data, policy, previous)
            if plan["holdout_end"] >= len(data.sessions):
                break
            current = self._study(name)
            champion = ranked_spec(json.loads(current["champion_json"]))
            end = data.sessions[plan["holdout_end"]]
            frozen = data.prefix(end)
            manifest = {
                "contract": "unified-research-v1",
                "study": name,
                "ordinal": ordinal,
                "build": build_id(),
                "policy": policy,
                "revision": current["revision"],
                "champion": champion,
                "data_id": frozen.fingerprint(end),
                "plan": plan,
                "dev_start": data.sessions[plan["dev_start"]],
                "dev_end": data.sessions[plan["dev_end"]],
                "holdout_start": data.sessions[plan["holdout_start"]],
                "holdout_end": end,
                "proposal_space": {
                    "lookbacks": policy.logic.lookbacks,
                    "rankings": policy.logic.rankings,
                },
                "benchmark": "frozen-incumbent-net-of-identical-costs",
                "positions_reset_per_evaluation": True,
                "statistical_policy": "paired-circular-block; Bonferroni across fixed cycle budget; indicative",
            }
            cycle_id = digest(manifest)
            with self.store.transaction():
                latest = self._study(name)
                if (
                    latest["revision"] != current["revision"]
                    or self.db.execute(
                        "SELECT 1 FROM agent_cycles WHERE study=? AND status IN ('REGISTERED','EVALUATED','FAILED')",
                        (name,),
                    ).fetchone()
                ):
                    raise AgentError("Another worker owns or changed this study")
                self._migrate_budget()
                account = self._budget_account(policy, name)
                if account["total_cycles"] != policy.max_validation_cycles:
                    raise AgentError(
                        "Validation budget belongs to the market strategy family; a different cycle budget needs an explicit allocation"
                    )
                if self._budget(policy)["reserved"] >= account["total_cycles"]:
                    raise AgentError(
                        "Validation budget exhausted for this market strategy family"
                    )
                self._budget_entry(account["account_id"], "reserve", cycle_id, name)
                self._checkpoint(name, data, end)
                self.db.execute(
                    "INSERT INTO agent_cycles VALUES(?,?,?,?,?,?,?,?)",
                    (
                        cycle_id,
                        name,
                        ordinal,
                        "REGISTERED",
                        canonical(manifest),
                        None,
                        None,
                        None,
                    ),
                )
                # Claim the reserved interval before any evaluation. Even a
                # crash or a no-challenger cycle cannot reopen this reservation.
                self._claim(
                    policy,
                    data.sessions[plan["holdout_start"]],
                    end,
                    cycle_id,
                    "holdout",
                    frozen,
                )
                self.db.execute(
                    "INSERT INTO evaluation_manifests VALUES(?,?)",
                    (cycle_id, canonical(manifest)),
                )
            try:
                evidence = self._evaluate(cycle_id, frozen, policy, champion, plan)
                with self.store.transaction():
                    self.db.execute(
                        "UPDATE agent_cycles SET status='EVALUATED',evidence_json=?,evidence_hash=? WHERE id=? AND status='REGISTERED'",
                        (canonical(evidence), digest(evidence), cycle_id),
                    )
            except Exception:
                with self.store.transaction():
                    self.db.execute(
                        "UPDATE agent_cycles SET status='FAILED' WHERE id=? AND status='REGISTERED'",
                        (cycle_id,),
                    )
                    self._consume_budget(cycle_id)
                raise
            completed.append(self._finish(cycle_id))
            previous = self.db.execute(
                "SELECT * FROM agent_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
        shadow = (
            self._shadow(name, data, policy, previous) if previous is not None else None
        )
        next_plan = self._plan(data, policy, previous)
        count = self.db.execute(
            "SELECT count(*) FROM agent_cycles WHERE study=?", (name,)
        ).fetchone()[0]
        return {
            "study": name,
            "cycles": completed,
            "shadow": shadow,
            "status": self.status(name),
            "waiting_for_data": next_plan["holdout_end"] >= len(data.sessions),
            "next_window_sessions_required": max(
                0, next_plan["holdout_end"] + 1 - len(data.sessions)
            ),
            "validation_budget_exhausted": count >= policy.max_validation_cycles
            or self._budget(policy)["remaining"] == 0,
            "orders_submitted": 0,
            "live_enabled": False,
        }

    def _evaluate(
        self,
        cycle_id: str,
        data: ResearchInput,
        policy: AgentPolicy,
        champion: Any,
        plan: dict[str, int],
    ) -> dict[str, Any]:
        evaluator = CommonEvaluator(policy)
        dev = data.prefix(data.sessions[plan["dev_end"]])
        proposer = LogicProposer(policy.logic)
        candidates = {s.version: s for s in [champion, *proposer.propose(champion)]}
        trials = []
        fold_size = policy.development_sessions // 3
        reference_folds: list[EvaluationResult] = []
        for spec in candidates.values():
            try:
                folds = [
                    evaluator.evaluate(
                        dev,
                        spec,
                        dev.sessions[plan["dev_start"] + i * fold_size],
                        dev.sessions[plan["dev_start"] + (i + 1) * fold_size - 1]
                        if i < 2
                        else dev.sessions[-1],
                    )
                    for i in range(3)
                ]
                if spec.version == champion.version:
                    reference_folds = folds
                else:
                    for reference, candidate_fold in zip(
                        reference_folds, folds, strict=True
                    ):
                        require_same_contract(reference, candidate_fold)
                trials.append(
                    {
                        "spec": asdict(spec),
                        "version": spec.version,
                        "status": "COMPLETE",
                        "score": statistics.mean(
                            statistics.mean(f.daily_returns) for f in folds
                        ),
                        "worst_drawdown": max(f.max_drawdown for f in folds),
                        "fold_trades": [f.trades for f in folds],
                        "folds": [
                            {
                                "start": f.start,
                                "end": f.end,
                                "net_return": f.net_return,
                                "drawdown": f.max_drawdown,
                                "trades": f.trades,
                                "evaluation_contract_id": f.evaluation_contract_id,
                            }
                            for f in folds
                        ],
                    }
                )
            except AgentError as exc:
                trials.append(
                    {
                        "spec": asdict(spec),
                        "version": spec.version,
                        "status": "FAILED",
                        "reason": str(exc),
                    }
                )
        baseline = next(t for t in trials if t["version"] == champion.version)
        if baseline["status"] != "COMPLETE":
            raise AgentError("Champion development failed; evidence cannot be promoted")
        eligible = [
            t
            for t in trials
            if t["status"] == "COMPLETE"
            and t["worst_drawdown"] <= policy.logic.evaluation.max_drawdown
            and min(t["fold_trades"]) >= policy.logic.evaluation.min_fold_trades
        ]
        best = (
            max(eligible, key=lambda t: (t["score"], t["version"]))
            if eligible
            else baseline
        )
        if best["score"] <= baseline["score"]:
            best = baseline
        evidence = {
            "cycle_id": cycle_id,
            "statistical_policy_version": policy.statistical_policy_version,
            "nominal_one_sided_alpha_per_cycle": policy.family_alpha
            / policy.max_validation_cycles
            / (2 if policy.statistical_policy_version == "v1" else 1),
            "tail_draws": policy.bootstrap_samples
            * policy.family_alpha
            / policy.max_validation_cycles
            / (2 if policy.statistical_policy_version == "v1" else 1),
            "champion_version": champion.version,
            "selected_version": best["version"],
            "selected_spec": best["spec"],
            "trials": trials,
            "proposal": proposer.metadata,
            "development": {
                "candidate_score": best["score"],
                "champion_score": baseline["score"],
                "worst_drawdown": best["worst_drawdown"],
                "fold_trades": best["fold_trades"],
                "evaluation_contract_ids": [
                    f.evaluation_contract_id for f in reference_folds
                ],
            },
            "holdout": {"evaluated": False},
        }
        start, end = (
            data.sessions[plan["holdout_start"]],
            data.sessions[plan["holdout_end"]],
        )
        if best["version"] != champion.version:
            selected = candidates[best["version"]]
            base = evaluator.evaluate(data, champion, start, end)
            candidate = evaluator.evaluate(data, selected, start, end)
            stressed_base = evaluator.evaluate(data, champion, start, end, stress=True)
            stressed_candidate = evaluator.evaluate(
                data, selected, start, end, stress=True
            )
            require_same_contract(base, candidate)
            require_same_contract(stressed_base, stressed_candidate)
            if [r["session"] for r in base.equity_curve] != [
                r["session"] for r in candidate.equity_curve
            ]:
                raise AgentError("Independent return dates are not aligned")
            evidence["holdout"] = {
                "evaluated": True,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "champion_return": base.net_return,
                "candidate_return": candidate.net_return,
                "interval": confidence_interval(
                    [
                        a - b
                        for a, b in zip(
                            candidate.daily_returns, base.daily_returns, strict=True
                        )
                    ],
                    policy,
                ),
                "candidate_drawdown": candidate.max_drawdown,
                "candidate_trades": candidate.trades,
                "stress_improvement": stressed_candidate.net_return
                - stressed_base.net_return,
                "observations": len(candidate.daily_returns),
                "evaluation_contract_id": base.evaluation_contract_id,
                "stress_evaluation_contract_id": stressed_base.evaluation_contract_id,
                "baseline": asdict(base),
                "candidate": asdict(candidate),
                "stressed_baseline": asdict(stressed_base),
                "stressed_candidate": asdict(stressed_candidate),
            }
            comparison = candidate
        else:
            comparison = evaluator.evaluate(data, champion, start, end)
            evidence["holdout"].update(
                {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "baseline": asdict(comparison),
                    "evaluation_contract_id": comparison.evaluation_contract_id,
                }
            )
        evidence["holdout"]["benchmarks"] = benchmark_report(
            evaluator,
            data,
            start,
            end,
            comparison,
            "candidate" if best["version"] != champion.version else "champion",
        )
        evidence["gates"] = promotion_gates(evidence, policy)
        return evidence

    def _finish(self, cycle_id: str) -> dict[str, Any]:
        with self.store.transaction():
            self._migrate_budget()
            row = self.db.execute(
                "SELECT * FROM agent_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
            if not row or row["status"] not in {"EVALUATED", "COMPLETE"}:
                raise AgentError(
                    "No complete stored evaluation to recover; never rerun a consumed interval"
                )
            manifest, evidence = (
                json.loads(row["manifest_json"]),
                json.loads(row["evidence_json"]),
            )
            frozen = self.db.execute(
                "SELECT manifest_json FROM evaluation_manifests WHERE run_id=?",
                (cycle_id,),
            ).fetchone()
            study = self._study(row["study"])
            if (
                digest(manifest) != cycle_id
                or not frozen
                or frozen[0] != row["manifest_json"]
                or digest(evidence) != row["evidence_hash"]
                or evidence["cycle_id"] != cycle_id
                or manifest["build"] != build_id()
                or canonical(policy_from_dict(manifest["policy"]))
                != canonical(policy_from_dict(json.loads(study["policy_json"])))
            ):
                raise AgentError("Stored evidence or frozen manifest changed")
            policy = policy_from_dict(manifest["policy"])
            gates = promotion_gates(evidence, policy)
            if gates != evidence["gates"]:
                raise AgentError("Stored promotion gate evidence changed")
            claim = self.db.execute(
                "SELECT * FROM agent_intervals WHERE market=? AND owner=? AND kind='holdout'",
                (policy.market, cycle_id),
            ).fetchone()
            if (
                not claim
                or claim["start"] != manifest["holdout_start"]
                or claim["end"] != manifest["holdout_end"]
            ):
                raise AgentError("Independent consumed interval evidence missing")
            promoted = all(gates.values())
            selected, incumbent = (
                ranked_spec(evidence["selected_spec"]),
                ranked_spec(manifest["champion"]),
            )
            allowed = {
                s.version
                for s in [incumbent, *LogicProposer(policy.logic).propose(incumbent)]
            }
            if (
                selected.version != evidence["selected_version"]
                or selected.version not in allowed
            ):
                raise AgentError("Selected candidate exceeded frozen proposal space")
            result = {
                "cycle_id": cycle_id,
                "ordinal": row["ordinal"],
                "study": row["study"],
                "verdict": "PROMOTE_RESEARCH_CHAMPION" if promoted else "KEEP_CHAMPION",
                "research_champion_updated": promoted,
                "champion_before": manifest["champion"],
                "champion_after": asdict(selected)
                if promoted
                else manifest["champion"],
                "revision_before": manifest["revision"],
                "revision_after": manifest["revision"] + int(promoted),
                "holdout_start": manifest["holdout_start"],
                "holdout_end": manifest["holdout_end"],
                "gates": gates,
                "evidence_hash": row["evidence_hash"],
                "orders_submitted": 0,
            }
            if row["status"] == "COMPLETE":
                if canonical(result) != row["result_json"]:
                    raise AgentError("Completed result diverges from stored evidence")
                return json.loads(canonical(result))
            if study["revision"] != manifest["revision"] or study[
                "champion_json"
            ] != canonical(manifest["champion"]):
                raise AgentError("Champion revision changed during research")
            if promoted:
                self.db.execute(
                    "UPDATE agent_studies SET revision=revision+1,champion_json=? WHERE name=?",
                    (canonical(selected), row["study"]),
                )
            self.db.execute(
                "UPDATE agent_cycles SET status='COMPLETE',result_json=? WHERE id=?",
                (canonical(result), cycle_id),
            )
            self._consume_budget(cycle_id)
            self.store.event(
                "agent:" + row["study"], None, "AGENT_RESEARCH_COMPLETED", result
            )
            return json.loads(canonical(result))

    def recover(self, cycle_id: str) -> dict[str, Any]:
        return self._finish(cycle_id)

    def abandon(self, cycle_id: str, reason: str) -> dict[str, Any]:
        if not reason.strip() or len(reason) > 500:
            raise AgentError(
                "Explicit abandonment reason up to 500 characters required"
            )
        with self.store.transaction():
            row = self.db.execute(
                "SELECT * FROM agent_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
            if (
                not row
                or row["status"] not in {"REGISTERED", "FAILED"}
                or row["evidence_json"]
            ):
                raise AgentError(
                    "Only an interrupted unevaluated cycle may be abandoned; recover completed evidence"
                )
            self.db.execute(
                "UPDATE agent_cycles SET status='ABANDONED',result_json=? WHERE id=?",
                (canonical({"reason": reason}), cycle_id),
            )
            self._consume_budget(cycle_id)
            self.store.event(
                "agent:" + row["study"],
                None,
                "AGENT_RESEARCH_ABANDONED",
                {"cycle_id": cycle_id, "reason": reason},
            )
        return {
            "cycle_id": cycle_id,
            "status": "ABANDONED",
            "claims_preserved": True,
            "orders_submitted": 0,
        }

    def _shadow(
        self, name: str, data: ResearchInput, policy: AgentPolicy, cycle: Any
    ) -> dict[str, Any] | None:
        if cycle["status"] != "COMPLETE":
            return None
        result = json.loads(cycle["result_json"])
        start_index = data.sessions.index(date.fromisoformat(result["holdout_end"])) + 1
        end_index = min(
            start_index + policy.shadow_sessions - 1, len(data.sessions) - 1
        )
        if end_index - start_index < 1:
            return None
        start, end = data.sessions[start_index], data.sessions[end_index]
        fingerprint = data.fingerprint(end)
        prior = self.db.execute(
            "SELECT * FROM agent_shadow WHERE cycle_id=?", (cycle["id"],)
        ).fetchone()
        if (
            prior
            and prior["end"] == end.isoformat()
            and prior["input_hash"] == fingerprint
        ):
            report = json.loads(prior["report_json"])
            if digest(report) != prior["report_hash"]:
                raise AgentError("Stored shadow evidence changed")
            return report
        with self.store.transaction():
            self._checkpoint(name, data, end)
            self._claim(
                policy, start, end, "shadow:" + cycle["id"], "shadow", data.prefix(end)
            )
        champion = ranked_spec(result["champion_after"])
        baseline = ranked_spec(result["champion_before"])
        evaluator = CommonEvaluator(policy)
        forward = evaluator.evaluate(data.prefix(end), champion, start, end)
        reference = evaluator.evaluate(data.prefix(end), baseline, start, end)
        require_same_contract(forward, reference)
        report = {
            "kind": "FRESH_FORWARD_SHADOW",
            "research_revision": result["revision_after"],
            "start": start,
            "end": end,
            "observations": len(forward.daily_returns),
            "positions_continued": False,
            "initial_cash": str(policy.logic.selection.capital_limit),
            "candidate": asdict(forward),
            "reference": asdict(reference),
            "net_advantage": forward.net_return - reference.net_return,
            "deterioration_detected": forward.max_drawdown
            > policy.logic.evaluation.max_drawdown
            or forward.net_return < reference.net_return,
            "promotion_authority": False,
            "orders_submitted": 0,
        }
        report["benchmarks"] = benchmark_report(
            evaluator, data.prefix(end), start, end, forward, "champion"
        )
        # Screening only uses the observed shadow prefix, never reserved final
        # data at the end of an input file that happened to arrive early.
        frozen = data.prefix(end)
        c = policy.logic.costs
        costs = MarketCosts(c.commission_bps, c.sell_tax_bps, c.slippage_bps)
        view = frozen.snapshot.view(session_time(end, 16), end, allow_missing=True)
        report["selection"] = select_candidates(
            champion,
            view,
            frozen.securities,
            policy.logic.selection,
            costs,
            session_time(end, 17),
        )
        report = json.loads(canonical(report))
        with self.store.transaction():
            self.db.execute(
                "INSERT INTO agent_shadow VALUES(?,?,?,?,?) ON CONFLICT(cycle_id) DO UPDATE SET end=excluded.end,input_hash=excluded.input_hash,report_json=excluded.report_json,report_hash=excluded.report_hash",
                (
                    cycle["id"],
                    end.isoformat(),
                    fingerprint,
                    canonical(report),
                    digest(report),
                ),
            )
        return report
