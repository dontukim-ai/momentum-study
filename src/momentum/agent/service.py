"""Persistent four-stage agent with reserved, non-overlapping forward windows."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
from trading_agent.contracts import AgentError, aware, canonical, digest, session_time
from trading_agent.logic import LogicProposer, ranked_spec
from trading_agent.selection import select_candidates
from trading_agent.store import Store

from momentum.config import SpecUndefinedError

from .contracts import (
    AgentPolicy,
    ResearchInput,
    build_id,
    policy_from_dict,
    require_observed,
)
from .evaluator import (
    BenchmarkUnavailable,
    CommonEvaluator,
    EvaluationResult,
    MarketCosts,
    enabled_benchmarks,
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
    results: dict[str, Any] = {}
    for kind in enabled_benchmarks(evaluator.policy):
        try:
            result = evaluator.evaluate_benchmark(data, kind, start, end)
        except BenchmarkUnavailable as exc:
            if kind != "market_etf":
                raise
            results[kind] = {
                "status": "UNAVAILABLE",
                "symbol": evaluator.policy.market_etf_symbol,
                "reason": str(exc),
                "reference_evaluation_contract_id": reference.evaluation_contract_id,
            }
            continue
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
        if kind == "market_etf":
            results[kind].update(
                {
                    "status": "EVALUATED",
                    "symbol": evaluator.policy.market_etf_symbol,
                    "allocation_rule": "daily_rebalance_under_existing_position_order_and_gross_limits",
                }
            )
    return results


FAMILY_LINEAGE_ID = "ranked-momentum"
EXCLUSIVE_KINDS = {"holdout", "shadow", "final_validation"}
FINAL_KINDS = EXCLUSIVE_KINDS - {"shadow"}


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_observation_releases (
 observation_release_id TEXT PRIMARY KEY, market TEXT NOT NULL, family_lineage_id TEXT NOT NULL,
 champion_revision INTEGER NOT NULL, spec_version TEXT NOT NULL,
 evidence_grade TEXT NOT NULL CHECK(evidence_grade IN ('E2','E3')),
 source_cycle_id TEXT NOT NULL, payload_json TEXT NOT NULL, approved_by TEXT NOT NULL,
 approved_at TEXT NOT NULL, order_authority INTEGER NOT NULL CHECK(order_authority = 0));
CREATE TABLE IF NOT EXISTS agent_release_events (
 event_id TEXT PRIMARY KEY, observation_release_id TEXT NOT NULL REFERENCES agent_observation_releases,
 event TEXT NOT NULL CHECK(event IN ('STOPPED')),
 reason TEXT NOT NULL CHECK(reason IN ('DEMOTION_CANDIDATE','DATA_FAILURE','HUMAN')),
 detail_json TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_champion_registry (
 market TEXT NOT NULL, family_lineage_id TEXT NOT NULL, revision INTEGER NOT NULL,
 spec_json TEXT NOT NULL, spec_version TEXT NOT NULL, evidence_grade TEXT,
 source_cycle_id TEXT, commitment_id TEXT, previous_revision INTEGER,
 study TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(market, family_lineage_id, revision));
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
CREATE TABLE IF NOT EXISTS agent_commitments (
 commitment_id TEXT PRIMARY KEY, study TEXT NOT NULL, ordinal INTEGER NOT NULL,
 payload_json TEXT NOT NULL, committed_at TEXT NOT NULL, UNIQUE(study, ordinal));
CREATE TABLE IF NOT EXISTS agent_cohorts (
 cohort_id TEXT PRIMARY KEY, market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
 commitment_id TEXT NOT NULL UNIQUE, baseline_version TEXT NOT NULL,
 observation_streams_json TEXT NOT NULL, registered_at TEXT NOT NULL,
 first_execution_at TEXT NOT NULL, registered_before_start INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS agent_cycles (
 id TEXT PRIMARY KEY, study TEXT NOT NULL REFERENCES agent_studies(name), ordinal INTEGER NOT NULL,
 status TEXT NOT NULL, manifest_json TEXT NOT NULL, evidence_json TEXT, evidence_hash TEXT, result_json TEXT,
 UNIQUE(study,ordinal));
CREATE UNIQUE INDEX IF NOT EXISTS agent_one_pending ON agent_cycles(study)
 WHERE status IN ('REGISTERED','EVALUATED','FAILED');
CREATE TABLE IF NOT EXISTS agent_intervals (
 market TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, owner TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(market,owner,kind));
CREATE TABLE IF NOT EXISTS agent_failure_snapshots (
 snapshot_id TEXT PRIMARY KEY, study TEXT NOT NULL, source_cycle_id TEXT NOT NULL,
 source_phase TEXT NOT NULL CHECK(source_phase IN ('development','final','observation')),
 failure_code TEXT NOT NULL, source_window_start TEXT NOT NULL, source_window_end TEXT NOT NULL,
 observed_through TEXT NOT NULL, payload_json TEXT NOT NULL, result_hash TEXT NOT NULL,
 sealed_at TEXT NOT NULL,
 UNIQUE(source_cycle_id, source_phase, failure_code, observed_through));
CREATE TABLE IF NOT EXISTS agent_memory_releases (
 snapshot_id TEXT PRIMARY KEY REFERENCES agent_failure_snapshots,
 released_at TEXT NOT NULL, rule TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_observation_checkpoints (
 checkpoint_id TEXT PRIMARY KEY, market TEXT NOT NULL, stream TEXT NOT NULL,
 study TEXT NOT NULL, seq INTEGER NOT NULL, window_start TEXT NOT NULL,
 window_end TEXT NOT NULL, revision INTEGER NOT NULL, spec_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('OBSERVING','WARNING','DEMOTION_CANDIDATE')),
 sealed INTEGER NOT NULL, payload_json TEXT NOT NULL, result_hash TEXT NOT NULL,
 input_hash TEXT NOT NULL, recorded_at TEXT NOT NULL, UNIQUE(stream, seq));
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


def _commitment_grade(committed_at: str, holdout_start: str) -> str:
    return (
        "E2"
        if datetime.fromisoformat(committed_at)
        < session_time(date.fromisoformat(holdout_start), 9)
        else "E1"
    )


class ResearchAgent:
    def __init__(self, store: Store, *, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(UTC))
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

    @staticmethod
    def _release_actor(value: str) -> None:
        if not isinstance(value, str) or not value.strip() or len(value) > 100:
            raise AgentError(
                "Release approver must be a nonblank string of at most 100 characters"
            )

    def _active_release(self, market: str) -> Any:
        return self.db.execute(
            "SELECT r.* FROM agent_observation_releases r WHERE market=? AND family_lineage_id=? "
            "AND NOT EXISTS (SELECT 1 FROM agent_release_events e WHERE e.observation_release_id=r.observation_release_id)",
            (market, FAMILY_LINEAGE_ID),
        ).fetchone()

    @staticmethod
    def _release_report(row: Any) -> dict[str, Any]:
        return {
            **json.loads(row["payload_json"]),
            **{
                k: row[k]
                for k in ("observation_release_id", "approved_by", "approved_at")
            },
        }

    def register_observation_release(
        self, market: str = "KRX", *, approved_by: str, revision: int | None = None
    ) -> dict[str, Any]:
        self._release_actor(approved_by)
        with self.store.transaction():
            self._migrate_registry()
            champion = self._registry_current(market)
            if revision is not None and (
                champion is None or revision != champion["revision"]
            ):
                raise AgentError(
                    "Observation release must target the current registry champion"
                )
            if champion is None or champion["evidence_grade"] not in {"E2", "E3"}:
                raise AgentError("Observation release requires E2 or stronger evidence")
            active = self._active_release(market)
            if active:
                if active["champion_revision"] == champion["revision"]:
                    return self._release_report(active)
                raise AgentError(
                    "Active observation release exists; stop it before registering another"
                )
            cycle = self.db.execute(
                "SELECT * FROM agent_cycles WHERE id=?", (champion["source_cycle_id"],)
            ).fetchone()
            if cycle is None:
                raise AgentError("Observation release requires source cycle evidence")
            evidence = json.loads(cycle["evidence_json"] or "{}")
            contract_id = (
                evidence.get("holdout", {})
                .get("candidate", {})
                .get("evaluation_contract_id")
            )
            if not contract_id:
                raise AgentError(
                    "Observation release requires final candidate evaluation contract ID"
                )
            policy = policy_from_dict(json.loads(cycle["manifest_json"])["policy"])
            selection = policy.logic.selection
            payload = {
                "champion_key": {
                    "market": market,
                    "family_lineage_id": FAMILY_LINEAGE_ID,
                },
                "champion_revision": champion["revision"],
                "spec": json.loads(champion["spec_json"]),
                "spec_version": champion["spec_version"],
                "evidence_grade": champion["evidence_grade"],
                "source_cycle_id": champion["source_cycle_id"],
                "commitment_id": champion["commitment_id"],
                "selector_policy_id": digest(selection),
                "spread_mode": selection.spread_mode,
                "execution_eligible": selection.spread_mode == "quoted",
                "cost_model_id": digest(policy.logic.costs),
                "risk_limits_id": digest(
                    {
                        **{
                            k: getattr(selection, k)
                            for k in (
                                "capital_limit",
                                "max_gross",
                                "max_position",
                                "max_order_notional",
                            )
                        },
                        "max_drawdown": policy.logic.evaluation.max_drawdown,
                    }
                ),
                "evaluation_contract_id": contract_id,
                "starting_portfolio": {
                    "kind": "cash_only",
                    "capital": str(selection.capital_limit),
                },
                "data_exposure": {
                    "through": self.db.execute(
                        "SELECT max(end) FROM agent_intervals WHERE market=?", (market,)
                    ).fetchone()[0]
                },
                "stop_conditions": ["DEMOTION_CANDIDATE", "DATA_FAILURE", "HUMAN"],
                "order_authority": False,
            }
            approved_at = aware(self.clock()).isoformat()
            release_id = digest(
                {**payload, "approved_by": approved_by, "approved_at": approved_at}
            )
            self.db.execute(
                "INSERT INTO agent_observation_releases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    release_id,
                    market,
                    FAMILY_LINEAGE_ID,
                    champion["revision"],
                    champion["spec_version"],
                    champion["evidence_grade"],
                    champion["source_cycle_id"],
                    canonical(payload),
                    approved_by,
                    approved_at,
                    0,
                ),
            )
            return self._release_report(self._active_release(market))

    def stop_observation_release(
        self,
        release_id: str,
        *,
        reason: str = "HUMAN",
        stopped_by: str,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._release_actor(stopped_by)
        if reason not in {"DEMOTION_CANDIDATE", "DATA_FAILURE", "HUMAN"}:
            raise AgentError("Invalid observation release stop reason")
        with self.store.transaction():
            row = self.db.execute(
                "SELECT * FROM agent_observation_releases r WHERE observation_release_id=? "
                "AND NOT EXISTS (SELECT 1 FROM agent_release_events e WHERE e.observation_release_id=r.observation_release_id)",
                (release_id,),
            ).fetchone()
            if row is None:
                raise AgentError("Observation release is not active")
            event = {
                "observation_release_id": release_id,
                "event": "STOPPED",
                "reason": reason,
                "detail": detail or {},
                "recorded_by": stopped_by,
                "recorded_at": aware(self.clock()).isoformat(),
            }
            event["event_id"] = digest(event)
            self.db.execute(
                "INSERT INTO agent_release_events VALUES(?,?,?,?,?,?,?)",
                (
                    event["event_id"],
                    release_id,
                    "STOPPED",
                    reason,
                    canonical(event["detail"]),
                    stopped_by,
                    event["recorded_at"],
                ),
            )
            return event

    def _release_status(self, market: str) -> dict[str, Any]:
        active = self._active_release(market)
        summary = None
        if active:
            summary = {
                k: active[k]
                for k in (
                    "observation_release_id",
                    "champion_revision",
                    "evidence_grade",
                    "approved_by",
                    "approved_at",
                )
            }
            summary["behind_registry"] = (
                self._registry_current(market)["revision"] > active["champion_revision"]
            )
        last = self.db.execute(
            "SELECT e.* FROM agent_release_events e JOIN agent_observation_releases r USING(observation_release_id) "
            "WHERE r.market=? AND r.family_lineage_id=? ORDER BY e.rowid DESC LIMIT 1",
            (market, FAMILY_LINEAGE_ID),
        ).fetchone()
        event = dict(last) if last else None
        if event:
            event["detail"] = json.loads(event.pop("detail_json"))
        return {"observation_release": summary, "last_release_event": event}

    def _automatic_release_stop(
        self,
        market: str,
        observation: dict[str, Any] | None,
        checkpoints: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        active = self._active_release(market)
        if active is None:
            return []
        failure = (
            observation
            if observation and observation.get("status") == "unavailable"
            else checkpoints
            if checkpoints and checkpoints.get("error")
            else None
        )
        if failure:
            return [
                self.stop_observation_release(
                    active["observation_release_id"],
                    reason="DATA_FAILURE",
                    stopped_by="agent",
                    detail={
                        "error_type": failure.get("error_type", "AgentError"),
                        "error": failure["error"],
                    },
                )
            ]
        # Read the revision from storage: sealed public reports intentionally omit it.
        checkpoint = self.db.execute(
            "SELECT * FROM agent_observation_checkpoints WHERE stream=? ORDER BY seq DESC LIMIT 1",
            (self._observation_stream(market),),
        ).fetchone()
        if (
            checkpoint
            and checkpoint["state"] == "DEMOTION_CANDIDATE"
            and checkpoint["revision"] == active["champion_revision"]
        ):
            detail = {k: checkpoint[k] for k in ("seq", "window_start", "window_end")}
            return [
                self.stop_observation_release(
                    active["observation_release_id"],
                    reason="DEMOTION_CANDIDATE",
                    stopped_by="agent",
                    detail=detail,
                )
            ]
        return []

    def _registry_current(self, market: str) -> Any:
        return self.db.execute(
            "SELECT * FROM agent_champion_registry WHERE market=? AND family_lineage_id=? "
            "ORDER BY revision DESC LIMIT 1",
            (market, FAMILY_LINEAGE_ID),
        ).fetchone()

    def _append_champion(
        self,
        market: str,
        revision: int,
        spec: Any,
        study: str,
        grade: str | None = None,
        cycle_id: str | None = None,
        commitment_id: str | None = None,
        previous_revision: int | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO agent_champion_registry VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                market,
                FAMILY_LINEAGE_ID,
                revision,
                canonical(spec),
                spec.version,
                grade,
                cycle_id,
                commitment_id,
                previous_revision,
                study,
                aware(self.clock()).isoformat(),
            ),
        )

    def _migrate_registry(self) -> None:
        # Preserve only the chosen current legacy state; fabricated past revisions
        # would imply evidence that the old study-local storage never retained.
        for row in self.db.execute(
            "SELECT s.* FROM agent_studies s WHERE EXISTS "
            "(SELECT 1 FROM agent_cycles c WHERE c.study=s.name AND c.status='COMPLETE') "
            "ORDER BY s.revision DESC,s.name ASC"
        ):
            market = json.loads(row["policy_json"])["market"]
            if self._registry_current(market) is None:
                self._append_champion(
                    market,
                    row["revision"],
                    ranked_spec(json.loads(row["champion_json"])),
                    row["name"],
                    "legacy",
                )

    def champion_registry(self, market: str = "KRX") -> list[dict[str, Any]]:
        with self.store.transaction():
            self._migrate_registry()
            history = []
            for row in self.db.execute(
                "SELECT * FROM agent_champion_registry WHERE market=? "
                "AND family_lineage_id=? ORDER BY revision",
                (market, FAMILY_LINEAGE_ID),
            ):
                entry = dict(row)
                entry["spec"] = json.loads(entry.pop("spec_json"))
                history.append(entry)
        return (
            [
                {
                    "key": {"market": market, "family_lineage_id": FAMILY_LINEAGE_ID},
                    "history": history,
                    "current": history[-1],
                }
            ]
            if history
            else []
        )

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
            self._migrate_registry()
            registry = self._registry_current(policy.market)
            champion, revision = policy.logic.champion, 0
            if not row and registry:
                try:
                    champion = ranked_spec(json.loads(registry["spec_json"]))
                    if (
                        champion.lookback not in policy.logic.lookbacks
                        or champion.ranking not in policy.logic.rankings
                        or champion.gross_weight > policy.logic.selection.max_gross
                        or champion.gross_weight / champion.top_n
                        > policy.logic.selection.max_position
                        or canonical(
                            {
                                k: v
                                for k, v in asdict(champion).items()
                                if k not in {"lookback", "ranking"}
                            }
                        )
                        != canonical(
                            {
                                k: v
                                for k, v in asdict(policy.logic.champion).items()
                                if k not in {"lookback", "ranking"}
                            }
                        )
                    ):
                        raise AgentError("Outside proposal space")
                except (AgentError, TypeError, ValueError, KeyError):
                    raise AgentError(
                        "Registry champion is outside this study's registered proposal space"
                    ) from None
                revision = registry["revision"]
            self.db.execute(
                "INSERT OR IGNORE INTO agent_studies VALUES(?,?,?,?,?,?,?)",
                (
                    name,
                    canonical(policy),
                    build_id(),
                    revision,
                    canonical(champion),
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
                account["account_id"],
                "reserve",
                manifest.get("commitment_id", cycle["id"]),
                cycle["study"],
            )
            if cycle["status"] not in {"REGISTERED", "EVALUATED"}:
                self._budget_entry(
                    account["account_id"],
                    "consume",
                    manifest.get("commitment_id", cycle["id"]),
                    cycle["study"],
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
        manifest = json.loads(cycle["manifest_json"])
        policy = policy_from_dict(manifest["policy"])
        self._budget_entry(
            self._budget_account_id(policy),
            "consume",
            manifest.get("commitment_id", cycle_id),
            cycle["study"],
        )

    def _cycle_grade(self, cycle: Any) -> str:
        evidence = json.loads(cycle["evidence_json"] or "{}")
        if "evidence_grade" in evidence:
            return str(evidence["evidence_grade"])
        commitment = self.db.execute(
            "SELECT * FROM agent_commitments WHERE study=? AND ordinal=?",
            (cycle["study"], cycle["ordinal"]),
        ).fetchone()
        if commitment:
            payload = json.loads(commitment["payload_json"])
            return _commitment_grade(
                commitment["committed_at"], payload["holdout_start"]
            )
        return "E1"

    def development_memory(self, market: str = "KRX") -> list[dict[str, Any]]:
        memories = []
        # SQLite rowid is the append sequence of this TEXT-primary-key table.
        # An inner join is the read permission: sealed alone never grants access.
        for row in self.db.execute(
            "SELECT s.*,r.released_at,t.policy_json FROM agent_failure_snapshots s "
            "JOIN agent_memory_releases r USING(snapshot_id) "
            "JOIN agent_studies t ON t.name=s.study ORDER BY s.rowid"
        ):
            if json.loads(row["policy_json"])["market"] != market:
                continue
            memory = dict(row)
            memory.pop("policy_json")
            memory["payload"] = json.loads(memory.pop("payload_json"))
            memory["eligible_for_proposer"] = True
            memory["consumed_windows"] = (
                []
                if row["source_phase"] == "development"
                else [
                    {
                        "market": market,
                        "start": row["source_window_start"],
                        "end": row["source_window_end"],
                    }
                ]
            )
            memories.append(memory)
        return memories

    def _seal_memory(
        self,
        name: str,
        source_cycle_id: str,
        phase: str,
        code: str,
        start: str,
        end: str,
        payload: dict[str, Any],
    ) -> None:
        # Callers own the decision transaction, so permission and the result
        # become visible atomically. Existing snapshots are never overwritten.
        result_hash = digest(payload)
        snapshot_id = digest(
            {
                "source_cycle_id": source_cycle_id,
                "source_phase": phase,
                "failure_code": code,
                "observed_through": end,
                "result_hash": result_hash,
            }
        )
        if self.db.execute(
            "SELECT 1 FROM agent_failure_snapshots WHERE source_cycle_id=? "
            "AND source_phase=? AND failure_code=? AND observed_through=?",
            (source_cycle_id, phase, code, end),
        ).fetchone():
            return
        now = aware(self.clock()).isoformat()
        self.db.execute(
            "INSERT INTO agent_failure_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                name,
                source_cycle_id,
                phase,
                code,
                start,
                end,
                end,
                canonical(payload),
                result_hash,
                now,
            ),
        )
        rule = {
            "development": "development_sealed",
            "final": "final_decision_confirmed",
            "observation": "observation_window_closed",
        }[phase]
        self.db.execute(
            "INSERT INTO agent_memory_releases VALUES(?,?,?)", (snapshot_id, now, rule)
        )

    def _seal_development(
        self,
        name: str,
        commitment_id: str,
        payload: dict[str, Any],
        policy: AgentPolicy,
    ) -> None:
        evidence = payload["development_evidence"]
        start = payload["plan_dates"]["dev_start"].isoformat()
        end = payload["plan_dates"]["dev_end"].isoformat()
        failed = [
            {"version": t["version"], "reason": t["reason"]}
            for t in evidence["trials"]
            if t["status"] == "FAILED"
        ]
        risky = [
            {
                "version": t["version"],
                "worst_drawdown": t["worst_drawdown"],
                "fold_trades": t["fold_trades"],
            }
            for t in evidence["trials"]
            if t["status"] == "COMPLETE"
            and (
                t["worst_drawdown"] > policy.logic.evaluation.max_drawdown
                or min(t["fold_trades"]) < policy.logic.evaluation.min_fold_trades
            )
        ]
        reasons: dict[str, dict[str, Any]] = {}
        if failed:
            reasons["DATA_DEFECT"] = {"trials": failed}
        if risky:
            reasons["DEV_RISK"] = {
                "trials": risky,
                "max_drawdown": policy.logic.evaluation.max_drawdown,
                "min_fold_trades": policy.logic.evaluation.min_fold_trades,
            }
        if evidence["selected_version"] == evidence["champion_version"]:
            reasons["DEV_NO_IMPROVEMENT"] = {
                "selected_version": evidence["selected_version"],
                "champion_version": evidence["champion_version"],
                **evidence["development"],
            }
        for code, reason in reasons.items():
            self._seal_memory(
                name, commitment_id, "development", code, start, end, reason
            )

    def status(self, name: str) -> dict[str, Any]:
        row = self._study(name)
        with self.store.transaction():
            self._migrate_budget()
            policy = policy_from_dict(json.loads(row["policy_json"]))
            budget = self._budget(policy)
            self._migrate_registry()
            registry = self._registry_current(policy.market)
        return {
            "study": name,
            "budget": budget,
            "registry": {
                "key": {
                    "market": policy.market,
                    "family_lineage_id": FAMILY_LINEAGE_ID,
                },
                "current_revision": registry["revision"] if registry else None,
                "current_spec_version": registry["spec_version"] if registry else None,
                "evidence_grade": registry["evidence_grade"] if registry else None,
            },
            "observation_state": self._latest_checkpoint(policy.market),
            **self._release_status(policy.market),
            "cohorts": [
                {
                    key: cohort[key]
                    for key in (
                        "cohort_id",
                        "start",
                        "end",
                        "registered_before_start",
                        "sealed",
                    )
                }
                for cohort in self._cohorts(policy.market)
                if cohort["study"] == name
            ],
            "memory": {
                "snapshots": self.db.execute(
                    "SELECT count(*) FROM agent_failure_snapshots WHERE study=?",
                    (name,),
                ).fetchone()[0],
                "released": self.db.execute(
                    "SELECT count(*) FROM agent_failure_snapshots JOIN agent_memory_releases "
                    "USING(snapshot_id) WHERE study=?",
                    (name,),
                ).fetchone()[0],
                "by_code": dict(
                    self.db.execute(
                        "SELECT failure_code,count(*) FROM agent_failure_snapshots "
                        "WHERE study=? GROUP BY failure_code",
                        (name,),
                    )
                ),
            },
            "mode": "AUTOMATED_RESEARCH_ONLY",
            "revision": row["revision"],
            "champion": json.loads(row["champion_json"]),
            "build": row["build"],
            "policy": json.loads(row["policy_json"]),
            "checkpoint_end": row["checkpoint_end"],
            "cycles": [
                {
                    "id": r["id"],
                    "ordinal": r["ordinal"],
                    "status": r["status"],
                    "evidence_grade": self._cycle_grade(r),
                }
                for r in self.db.execute(
                    "SELECT id,study,ordinal,status,evidence_json FROM agent_cycles WHERE study=? ORDER BY ordinal",
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

    @staticmethod
    def _observation_stream(market: str) -> str:
        return f"observation:{market}:{FAMILY_LINEAGE_ID}"

    def _cohorts(self, market: str) -> list[dict[str, Any]]:
        # Ownership moves to the cycle at evaluation registration; the cohort
        # always retains its original, immutable commitment reference.
        return [
            {**dict(row), "sealed": row["status"] not in ("COMPLETE", "ABANDONED")}
            for row in self.db.execute(
                "SELECT h.*, c.study, c.ordinal, y.id AS cycle_id, y.status "
                "FROM agent_cohorts h JOIN agent_commitments c USING(commitment_id) "
                "LEFT JOIN agent_cycles y ON y.study=c.study AND y.ordinal=c.ordinal "
                "WHERE h.market=? ORDER BY h.start,h.cohort_id",
                (market,),
            )
        ]

    def _cohort_covers(
        self, market: str, owner: str, stream: str, start: str, end: str
    ) -> bool:
        return any(
            owner in (cohort["commitment_id"], cohort["cycle_id"])
            and cohort["start"] <= start <= end <= cohort["end"]
            and stream in json.loads(cohort["observation_streams_json"])
            for cohort in self._cohorts(market)
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
        if kind not in EXCLUSIVE_KINDS | {"observation_exposure"}:
            raise AgentError("Unsupported evaluation interval kind")
        existing = self.db.execute(
            "SELECT start,end FROM agent_intervals WHERE market=? AND owner=? AND kind=?",
            (policy.market, owner, kind),
        ).fetchone()
        # Validate the entire resulting observation interval, including gaps
        # crossed by an extension. The stream's original start never moves.
        extending_observation = kind == "observation_exposure" and existing is not None
        claim_start = existing["start"] if extending_observation else start.isoformat()
        claim_end = (
            max(end.isoformat(), existing["end"])
            if extending_observation
            else end.isoformat()
        )
        args = (policy.market, owner, claim_start, claim_end)
        overlaps = list(
            self.db.execute(
                "SELECT * FROM agent_intervals WHERE market=? AND owner<>? AND NOT(end<? OR start>?)",
                args,
            )
        )
        if kind in EXCLUSIVE_KINDS:
            if any(row["kind"] in EXCLUSIVE_KINDS for row in overlaps):
                raise AgentError(
                    "Evaluation interval overlaps consumed market evidence"
                )
            if kind in FINAL_KINDS and any(
                row["kind"] == "observation_exposure" for row in overlaps
            ):
                # A newly registered cohort cannot retroactively legitimize
                # observation already exposed before the final reservation.
                raise AgentError(
                    "Final validation window overlaps observation exposure without a registered cohort"
                )
        else:
            for row in overlaps:
                if row["kind"] in FINAL_KINDS and not self._cohort_covers(
                    policy.market,
                    row["owner"],
                    owner,
                    max(claim_start, row["start"]),
                    min(claim_end, row["end"]),
                ):
                    raise AgentError(
                        "Observation exposure overlaps final validation outside its cohort"
                    )
        # Legacy KIS mirrors final/shadow claims. Unmatched legacy evidence is
        # still final consumption; only a matching shadow or cohort may permit
        # an observation stream to overlap it.
        legacy = self.db.execute(
            "SELECT * FROM holdouts WHERE run_id<>? AND NOT(end<? OR start>?)", args[1:]
        )
        for row in legacy:
            if kind in EXCLUSIVE_KINDS:
                raise AgentError(
                    "Evaluation interval overlaps consumed legacy KIS evidence"
                )
            overlap_start = max(claim_start, row["start"])
            overlap_end = min(claim_end, row["end"])
            shadow = self.db.execute(
                "SELECT 1 FROM agent_intervals WHERE market=? AND owner=? AND kind='shadow' "
                "AND start<=? AND end>=?",
                (policy.market, row["run_id"], overlap_start, overlap_end),
            ).fetchone()
            if not shadow and not self._cohort_covers(
                policy.market, row["run_id"], owner, overlap_start, overlap_end
            ):
                raise AgentError(
                    "Observation exposure overlaps final validation outside its cohort"
                )
        self.db.execute(
            "INSERT INTO agent_intervals VALUES(?,?,?,?,?) ON CONFLICT(market,owner,kind) DO UPDATE SET end=MAX(end,excluded.end)",
            (policy.market, start.isoformat(), end.isoformat(), owner, kind),
        )
        if kind in EXCLUSIVE_KINDS:
            self.db.executemany(
                "INSERT OR IGNORE INTO holdouts VALUES(?,?,?,?)",
                [
                    (symbol, start.isoformat(), end.isoformat(), owner)
                    for symbol in data.snapshot.symbols
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
                data.calendar.index(date.fromisoformat(old["holdout_end"]))
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
        require_observed(data, now=self.clock())
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
        committed_cycle = None
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
            commitment = self.db.execute(
                "SELECT * FROM agent_commitments WHERE study=? AND ordinal=?",
                (name, ordinal),
            ).fetchone()
            current = self._study(name)
            if commitment is None:
                if plan["dev_end"] >= len(data.sessions) or plan["holdout_end"] >= len(
                    data.calendar
                ):
                    break
                champion = ranked_spec(json.loads(current["champion_json"]))
                dev_end = data.calendar[plan["dev_end"]]
                dev = data.prefix(dev_end)
                development = self._develop(dev, policy, champion, plan)
                payload = {
                    "study": name,
                    "ordinal": ordinal,
                    "plan": plan,
                    "plan_dates": {
                        key: data.calendar[index] for key, index in plan.items()
                    },
                    "build": build_id(),
                    "policy": policy,
                    "revision": current["revision"],
                    "champion": champion,
                    "champion_version": champion.version,
                    "selected_spec": development["selected_spec"]
                    if development["selected_version"] != champion.version
                    else None,
                    "selected_version": development["selected_version"]
                    if development["selected_version"] != champion.version
                    else None,
                    "development_evidence": development,
                    "promotion_gate": policy.logic.evaluation,
                    "statistical_policy_version": policy.statistical_policy_version,
                    "holdout_start": data.calendar[plan["holdout_start"]],
                    "holdout_end": data.calendar[plan["holdout_end"]],
                    "holdout_calendar_hash": digest(
                        data.calendar[plan["holdout_start"] : plan["holdout_end"] + 1]
                    ),
                    "development_fingerprint": dev.fingerprint(dev_end),
                }
                with self.store.transaction():
                    latest = self._study(name)
                    if (
                        latest["revision"] != current["revision"]
                        or self.db.execute(
                            "SELECT 1 FROM agent_commitments WHERE study=? AND ordinal=?",
                            (name, ordinal),
                        ).fetchone()
                    ):
                        raise AgentError("Another worker owns or changed this study")
                    self._migrate_registry()
                    registry = self._registry_current(policy.market)
                    if registry is not None:
                        if (
                            latest["revision"] != registry["revision"]
                            or latest["champion_json"] != registry["spec_json"]
                        ):
                            raise AgentError(
                                "Market strategy family champion changed; synchronize the study from the registry"
                            )
                    else:
                        self._append_champion(policy.market, 0, champion, name)
                    payload["proposer_inputs"] = [
                        m["snapshot_id"] for m in self.development_memory(policy.market)
                    ]
                    commitment_id = digest(payload)
                    self._migrate_budget()
                    account = self._budget_account(policy, name)
                    if account["total_cycles"] != policy.max_validation_cycles:
                        raise AgentError(
                            "Validation budget belongs to the market strategy family; a different cycle budget needs an explicit allocation"
                        )
                    if self._budget(policy)["remaining"] <= 0:
                        raise AgentError(
                            "Validation budget exhausted for this market strategy family"
                        )
                    self._budget_entry(
                        account["account_id"], "reserve", commitment_id, name
                    )
                    self._checkpoint(name, data, dev_end)
                    self._claim(
                        policy,
                        payload["holdout_start"],
                        payload["holdout_end"],
                        commitment_id,
                        "holdout",
                        dev,
                    )
                    committed_at = aware(self.clock()).isoformat()
                    self.db.execute(
                        "INSERT INTO agent_commitments VALUES(?,?,?,?,?)",
                        (
                            commitment_id,
                            name,
                            ordinal,
                            canonical(payload),
                            committed_at,
                        ),
                    )
                    cohort = {
                        "market": policy.market,
                        "start": payload["holdout_start"],
                        "end": payload["holdout_end"],
                        "commitment_id": commitment_id,
                        "baseline_version": champion.version,
                        "observation_streams": [
                            self._observation_stream(policy.market)
                        ],
                    }
                    registered_at = aware(self.clock())
                    first_execution_at = session_time(payload["holdout_start"], 9)
                    self.db.execute(
                        "INSERT INTO agent_cohorts VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            digest(cohort),
                            policy.market,
                            payload["holdout_start"].isoformat(),
                            payload["holdout_end"].isoformat(),
                            commitment_id,
                            champion.version,
                            canonical(cohort["observation_streams"]),
                            registered_at.isoformat(),
                            first_execution_at.isoformat(),
                            int(registered_at < first_execution_at),
                        ),
                    )
                    self._seal_development(name, commitment_id, payload, policy)
                commitment = self.db.execute(
                    "SELECT * FROM agent_commitments WHERE commitment_id=?",
                    (commitment_id,),
                ).fetchone()
            payload = json.loads(commitment["payload_json"])
            commitment_id = commitment["commitment_id"]
            if digest(payload) != commitment_id:
                raise AgentError("Stored commitment changed")
            plan = payload["plan"]
            # Check the published calendar before checkpoints or positional reads:
            # removing a final session must not silently shift the reserved window.
            final_dates = tuple(
                d
                for d in data.calendar
                if payload["holdout_start"] <= d.isoformat() <= payload["holdout_end"]
            )
            if (
                digest(final_dates) != payload["holdout_calendar_hash"]
                or digest(
                    data.calendar[plan["holdout_start"] : plan["holdout_end"] + 1]
                )
                != payload["holdout_calendar_hash"]
            ):
                raise AgentError("Exchange calendar changed after commitment")
            dev_end = date.fromisoformat(payload["plan_dates"]["dev_end"])
            if data.fingerprint(dev_end) != payload["development_fingerprint"]:
                raise AgentError("Development data changed after commitment")
            grade = _commitment_grade(
                commitment["committed_at"], payload["holdout_start"]
            )
            if data.sessions[-1] < date.fromisoformat(payload["holdout_end"]):
                committed_cycle = {
                    "ordinal": ordinal,
                    "commitment_id": commitment_id,
                    "evidence_grade_if_on_time": grade,
                    "holdout_start": payload["holdout_start"],
                    "sessions_required": max(
                        0, plan["holdout_end"] + 1 - len(data.sessions)
                    ),
                }
                break
            observed_dates = tuple(
                d
                for d in data.sessions
                if payload["holdout_start"] <= d.isoformat() <= payload["holdout_end"]
            )
            if digest(observed_dates) != payload["holdout_calendar_hash"]:
                raise AgentError("Exchange calendar changed after commitment")
            if (
                payload["build"] != build_id()
                or canonical(payload["policy"]) != canonical(policy)
                or payload["revision"] != current["revision"]
                or canonical(payload["champion"]) != current["champion_json"]
            ):
                raise AgentError("Frozen commitment policy or champion changed")
            champion = ranked_spec(payload["champion"])
            end = date.fromisoformat(payload["holdout_end"])
            frozen = data.prefix(end)
            manifest = {
                "contract": "unified-research-v1",
                "commitment_id": commitment_id,
                "study": name,
                "ordinal": ordinal,
                "build": build_id(),
                "policy": policy,
                "revision": current["revision"],
                "champion": champion,
                "data_id": frozen.fingerprint(end),
                "plan": plan,
                "dev_start": data.calendar[plan["dev_start"]],
                "dev_end": data.calendar[plan["dev_end"]],
                "holdout_start": data.calendar[plan["holdout_start"]],
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
                self._checkpoint(name, data, end)
                # Transfer ownership without releasing or changing reserved dates.
                # Legacy readers identify registered final evidence by cycle ID.
                claim = self.db.execute(
                    "SELECT * FROM agent_intervals WHERE market=? AND owner=? AND kind='holdout'",
                    (policy.market, commitment_id),
                ).fetchone()
                if (
                    not claim
                    or claim["start"] != payload["holdout_start"]
                    or claim["end"] != payload["holdout_end"]
                ):
                    raise AgentError("Independent consumed interval evidence missing")
                self.db.execute(
                    "UPDATE agent_intervals SET owner=? WHERE market=? AND owner=? AND kind='holdout'",
                    (cycle_id, policy.market, commitment_id),
                )
                self.db.execute(
                    "UPDATE holdouts SET run_id=? WHERE run_id=?",
                    (cycle_id, commitment_id),
                )
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
        observation = self._isolated_observation(name, data)
        observation_checkpoints = None
        if observation is not None and observation["status"] == "ok":
            try:
                with self.store.transaction():
                    self._claim(
                        policy,
                        date.fromisoformat(observation["start"]),
                        date.fromisoformat(observation["end"]),
                        self._observation_stream(policy.market),
                        "observation_exposure",
                        data,
                    )
                observation["exposure_recorded"] = True
            except AgentError as exc:
                observation["exposure_recorded"] = False
                observation["exposure_error"] = str(exc)
                observation_checkpoints = {
                    "stored": 0,
                    "latest": self._latest_checkpoint(policy.market),
                    "error": str(exc),
                }
            else:
                observation_checkpoints = self._isolated_checkpoints(
                    name, data, policy, observation
                )
        release_events = self._automatic_release_stop(
            policy.market, observation, observation_checkpoints
        )
        return {
            "study": name,
            "release_events": release_events,
            "cycles": completed,
            **({"committed_cycle": committed_cycle} if committed_cycle else {}),
            "shadow": shadow,
            "observation": observation,
            "observation_checkpoints": observation_checkpoints,
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

    def _develop(
        self,
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
        return evidence

    def _evaluate(
        self,
        cycle_id: str,
        data: ResearchInput,
        policy: AgentPolicy,
        champion: Any,
        plan: dict[str, int],
    ) -> dict[str, Any]:
        cycle = self.db.execute(
            "SELECT manifest_json FROM agent_cycles WHERE id=?", (cycle_id,)
        ).fetchone()
        manifest = json.loads(cycle[0])
        commitment = self.db.execute(
            "SELECT * FROM agent_commitments WHERE commitment_id=?",
            (manifest["commitment_id"],),
        ).fetchone()
        payload = json.loads(commitment["payload_json"])
        evidence = payload["development_evidence"]
        first_execution = session_time(date.fromisoformat(payload["holdout_start"]), 9)
        evidence.update(
            {
                "cycle_id": cycle_id,
                "commitment_id": commitment["commitment_id"],
                "committed_at": commitment["committed_at"],
                "first_execution_at": first_execution.isoformat(),
                "evidence_grade": _commitment_grade(
                    commitment["committed_at"], payload["holdout_start"]
                ),
            }
        )
        evaluator = CommonEvaluator(policy)
        start, end = (
            date.fromisoformat(payload["holdout_start"]),
            date.fromisoformat(payload["holdout_end"]),
        )
        if evidence["selected_version"] != champion.version:
            selected = ranked_spec(evidence["selected_spec"])
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
            "candidate"
            if evidence["selected_version"] != champion.version
            else "champion",
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
            if "commitment_id" in manifest:
                commitment = self.db.execute(
                    "SELECT * FROM agent_commitments WHERE commitment_id=?",
                    (manifest["commitment_id"],),
                ).fetchone()
                if not commitment:
                    raise AgentError("Frozen commitment missing")
                payload = json.loads(commitment["payload_json"])
                development = payload["development_evidence"]
                first_execution = session_time(
                    date.fromisoformat(payload["holdout_start"]), 9
                )
                grade = _commitment_grade(
                    commitment["committed_at"], payload["holdout_start"]
                )
                if (
                    digest(payload) != commitment["commitment_id"]
                    or evidence["commitment_id"] != commitment["commitment_id"]
                    or evidence["committed_at"] != commitment["committed_at"]
                    or evidence["first_execution_at"] != first_execution.isoformat()
                    or evidence["evidence_grade"] != grade
                    or any(
                        canonical(evidence[key]) != canonical(development[key])
                        for key in development
                        if key != "holdout"
                    )
                    or manifest["holdout_start"] != payload["holdout_start"]
                    or manifest["holdout_end"] != payload["holdout_end"]
                    or canonical(manifest["champion"]) != canonical(payload["champion"])
                    or canonical(manifest["policy"]) != canonical(payload["policy"])
                ):
                    raise AgentError(
                        "Stored evaluation diverges from frozen commitment"
                    )
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
                **(
                    {"evidence_grade": evidence["evidence_grade"]}
                    if "evidence_grade" in evidence
                    else {}
                ),
                "orders_submitted": 0,
            }
            if row["status"] == "COMPLETE":
                if canonical(result) != row["result_json"]:
                    raise AgentError("Completed result diverges from stored evidence")
                return json.loads(canonical(result))
            self._migrate_registry()
            registry = self._registry_current(policy.market)
            if registry is None or registry["revision"] != manifest["revision"]:
                raise AgentError("Champion revision changed during research")
            if study["revision"] != manifest["revision"] or study[
                "champion_json"
            ] != canonical(manifest["champion"]):
                raise AgentError("Champion revision changed during research")
            if promoted:
                self._append_champion(
                    policy.market,
                    registry["revision"] + 1,
                    selected,
                    row["study"],
                    self._cycle_grade(row),
                    cycle_id,
                    manifest.get("commitment_id"),
                    registry["revision"],
                )
                self.db.execute(
                    "UPDATE agent_studies SET revision=revision+1,champion_json=? WHERE name=?",
                    (canonical(selected), row["study"]),
                )
            self.db.execute(
                "UPDATE agent_cycles SET status='COMPLETE',result_json=? WHERE id=?",
                (canonical(result), cycle_id),
            )
            for code, keys in {
                "FINAL_UNDERPERFORM": ("net_improvement", "cost_stress"),
                "INSUFFICIENT_EVIDENCE": ("confidence", "independent_observations"),
            }.items():
                if any(gates.get(key) is False for key in keys):
                    h = evidence["holdout"]
                    self._seal_memory(
                        row["study"],
                        cycle_id,
                        "final",
                        code,
                        manifest["holdout_start"],
                        manifest["holdout_end"],
                        {
                            "selected_version": evidence["selected_version"],
                            "gates": {key: gates[key] for key in keys},
                            "metrics": {
                                key: h[key]
                                for key in (
                                    "champion_return",
                                    "candidate_return",
                                    "stress_improvement",
                                    "interval",
                                    "observations",
                                )
                            },
                        },
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

    def _checkpoint_sealed(self, market: str, start: str, end: str) -> bool:
        stream = self._observation_stream(market)
        return any(
            cohort["sealed"]
            and stream in json.loads(cohort["observation_streams_json"])
            and cohort["start"] <= end
            and cohort["end"] >= start
            for cohort in self._cohorts(market)
        )

    def _latest_checkpoint(self, market: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM agent_observation_checkpoints WHERE stream=? ORDER BY seq DESC LIMIT 1",
            (self._observation_stream(market),),
        ).fetchone()
        if row is None:
            return None
        # The stored flag records sealing at insertion. Disclosure and memory
        # permission follow the current cohort decisions without updating it.
        sealed = self._checkpoint_sealed(market, row["window_start"], row["window_end"])
        report = {
            key: row[key] for key in ("seq", "window_start", "window_end", "state")
        }
        report["sealed"] = sealed
        if not sealed:
            report.update(json.loads(row["payload_json"]))
        return report

    @staticmethod
    def _checkpoint_window(
        result: EvaluationResult, offset: int, count: int, capital: Decimal
    ) -> tuple[float, float]:
        curve = result.equity_curve
        base = Decimal(curve[offset - 1]["equity"]) if offset else capital
        peak, drawdown = base, Decimal(0)
        for row in curve[offset : offset + count]:
            nav = Decimal(row["equity"])
            peak = max(peak, nav)
            drawdown = max(drawdown, 1 - nav / peak)
        return float(Decimal(curve[offset + count - 1]["equity"]) / base - 1), float(
            drawdown
        )

    def _isolated_checkpoints(
        self,
        name: str,
        data: ResearchInput,
        policy: AgentPolicy,
        observation: dict[str, Any],
    ) -> dict[str, Any]:
        stored = 0
        error = None
        try:
            start = date.fromisoformat(observation["start"])
            days = [
                d
                for d in data.sessions
                if start <= d <= date.fromisoformat(observation["end"])
            ]
            entries = observation["schedule"]
            registry = {
                row["revision"]: dict(row)
                for row in self.db.execute(
                    "SELECT * FROM agent_champion_registry WHERE market=? AND family_lineage_id=?",
                    (policy.market, FAMILY_LINEAGE_ID),
                )
            }
            schedule = [
                (
                    date.fromisoformat(entry["effective_from"]),
                    ranked_spec(json.loads(registry[entry["revision"]]["spec_json"])),
                )
                for entry in entries
            ]
            stream = self._observation_stream(policy.market)
            size = policy.observation_checkpoint_sessions
            previous: dict[str, Any] | None = None
            evaluator = CommonEvaluator(policy)
            for seq in range(len(days) // size):
                offset = seq * size
                window_start, end = days[offset], days[offset + size - 1]
                # Restart from the observation origin, not from a fresh wallet
                # at each window: holdings and revision-switch costs carry over.
                frozen = data.prefix(end)
                result = evaluator.evaluate_schedule(frozen, schedule, start, end)
                reference = evaluator.evaluate_benchmark(
                    frozen, policy.degradation_reference, start, end
                )
                require_same_contract(result, reference)
                capital = policy.logic.selection.capital_limit
                net, drawdown = self._checkpoint_window(result, offset, size, capital)
                reference_net, _ = self._checkpoint_window(
                    reference, offset, size, capital
                )
                active = next(
                    e
                    for e in reversed(entries)
                    if e["effective_from"] <= end.isoformat()
                )
                revision = active["revision"]
                limit = policy.degradation_drawdown
                if limit is None:
                    limit = policy.logic.evaluation.max_drawdown
                difference = net - reference_net
                breach = (
                    drawdown > limit
                    or difference < -policy.degradation_underperformance
                )
                consecutive = 0
                if breach:
                    consecutive = 1
                    if (
                        previous
                        and previous["revision"] == revision
                        and previous["breach"]
                    ):
                        consecutive += previous["consecutive_breaches"]
                state = "OBSERVING"
                if breach:
                    state = (
                        "DEMOTION_CANDIDATE"
                        if consecutive >= policy.demotion_checkpoints
                        else "WARNING"
                    )
                older = [r for r in registry if r < revision]
                target = registry[max(older)] if older else None
                payload = {
                    "window_start": window_start.isoformat(),
                    "window_end": end.isoformat(),
                    "revision": revision,
                    "spec_version": active["spec_version"],
                    "window_return": net,
                    "window_drawdown": drawdown,
                    "reference": policy.degradation_reference,
                    "reference_return": reference_net,
                    "difference": difference,
                    "drawdown_limit": limit,
                    "underperformance_threshold": policy.degradation_underperformance,
                    "demotion_checkpoints": policy.demotion_checkpoints,
                    "breach": breach,
                    "consecutive_breaches": consecutive,
                    "demotion_target": {
                        key: target[key] for key in ("revision", "spec_version")
                    }
                    if target
                    else None,
                }
                input_hash, result_hash = data.fingerprint(end), digest(payload)
                checkpoint_id = digest(
                    {"stream": stream, "seq": seq, "result_hash": result_hash}
                )
                sealed = self._checkpoint_sealed(
                    policy.market, window_start.isoformat(), end.isoformat()
                )
                with self.store.transaction():
                    existing = self.db.execute(
                        "SELECT * FROM agent_observation_checkpoints WHERE stream=? AND seq=?",
                        (stream, seq),
                    ).fetchone()
                    if existing:
                        if (
                            existing["input_hash"] != input_hash
                            or existing["result_hash"] != result_hash
                        ):
                            raise AgentError("Stored observation checkpoint changed")
                    else:
                        self.db.execute(
                            "INSERT INTO agent_observation_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                checkpoint_id,
                                policy.market,
                                stream,
                                name,
                                seq,
                                window_start.isoformat(),
                                end.isoformat(),
                                revision,
                                active["spec_version"],
                                state,
                                int(sealed),
                                canonical(payload),
                                result_hash,
                                input_hash,
                                aware(self.clock()).isoformat(),
                            ),
                        )
                    if breach and not sealed:
                        self._seal_memory(
                            existing["study"] if existing else name,
                            checkpoint_id,
                            "observation",
                            "TRACKING_" + state,
                            window_start.isoformat(),
                            end.isoformat(),
                            payload,
                        )
                stored += int(existing is None)
                previous = payload
        except (AgentError, SpecUndefinedError) as exc:
            error = str(exc)
            error_type = type(exc).__name__
        return {
            "stored": stored,
            "latest": self._latest_checkpoint(policy.market),
            **({"error": error, "error_type": error_type} if error else {}),
        }

    def _isolated_observation(
        self, name: str, data: ResearchInput
    ) -> dict[str, Any] | None:
        # Observation is report-only. A data defect inside its window must not
        # stop registration, evaluation or champion updates of the research loop;
        # it is reported instead. Programming errors still propagate.
        try:
            return self.observation_ledger(name, data)
        except (AgentError, SpecUndefinedError) as exc:
            return {
                "kind": "CONTINUOUS_OBSERVATION_LEDGER",
                "status": "unavailable",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "persisted": False,
                "promotion_authority": False,
                "orders_submitted": 0,
                "proposer_visible": False,
            }

    def _observation_limit(
        self, name: str, data: ResearchInput, policy: AgentPolicy
    ) -> dict[str, Any]:
        end = data.sessions[-1]
        limit: dict[str, Any] = {
            "end": end,
            "reason": "data_end",
            "next_holdout_start": None,
        }
        if self._budget(policy)["remaining"] <= 0:
            return limit
        previous = None
        for ordinal in range(policy.max_validation_cycles):
            cycle = self.db.execute(
                "SELECT * FROM agent_cycles WHERE study=? AND ordinal=?",
                (name, ordinal),
            ).fetchone()
            if cycle and cycle["status"] in ("COMPLETE", "ABANDONED"):
                previous = cycle
                continue
            commitment = self.db.execute(
                "SELECT payload_json FROM agent_commitments WHERE study=? AND ordinal=?",
                (name, ordinal),
            ).fetchone()
            if commitment:
                previous = {"manifest_json": commitment["payload_json"]}
                continue
            plan = self._plan(data, policy, previous)
            if plan["holdout_start"] < len(data.calendar):
                next_start = data.calendar[plan["holdout_start"]]
                limit["next_holdout_start"] = next_start
                if next_start <= end:
                    limit.update(
                        end=next_start - timedelta(days=1),
                        reason="next_uncommitted_holdout",
                    )
            break
        return limit

    def observation_ledger(
        self, name: str, data: ResearchInput
    ) -> dict[str, Any] | None:
        study = self._study(name)
        policy = policy_from_dict(json.loads(study["policy_json"]))
        first = self.db.execute(
            "SELECT * FROM agent_cycles WHERE study=? AND status='COMPLETE' "
            "ORDER BY ordinal LIMIT 1",
            (name,),
        ).fetchone()
        if first is None:
            return None

        def following(value: str) -> date | None:
            return next(
                (d for d in data.calendar if d > date.fromisoformat(value)), None
            )

        start = following(json.loads(first["result_json"])["holdout_end"])
        if start is None:
            return None
        rows = list(
            self.db.execute(
                "SELECT * FROM agent_champion_registry WHERE market=? "
                "AND family_lineage_id=? ORDER BY revision",
                (policy.market, FAMILY_LINEAGE_ID),
            )
        )
        # Read legacy state without triggering the registry's writing migration.
        if not rows:
            rows = [
                {
                    "revision": study["revision"],
                    "spec_json": study["champion_json"],
                    "source_cycle_id": None,
                }
            ]
        entries = []
        schedule = []
        for index, stored_row in enumerate(rows):
            row = dict(stored_row)
            effective: date | None = start
            cycle = None
            if row["source_cycle_id"]:
                cycle = self.db.execute(
                    "SELECT result_json FROM agent_cycles WHERE id=? AND status='COMPLETE'",
                    (row["source_cycle_id"],),
                ).fetchone()
            if cycle:
                effective = following(json.loads(cycle[0])["holdout_end"])
                if effective is None:
                    continue
            elif (
                index > 0
                or row["revision"] != 0
                or row.get("evidence_grade") == "legacy"
            ):
                # Unknown legacy ancestry cannot be reconstructed before this row.
                legacy = self.db.execute(
                    "SELECT result_json FROM agent_cycles WHERE study=? AND status='COMPLETE' "
                    "ORDER BY ordinal DESC LIMIT 1",
                    (row.get("study", name),),
                ).fetchone()
                if legacy is None:
                    return None
                effective = following(json.loads(legacy[0])["holdout_end"])
                if effective is None:
                    return None
                start = max(start, effective)
            if effective is None:
                continue
            spec = ranked_spec(json.loads(row["spec_json"]))
            schedule.append((effective, spec))
            entries.append(
                {
                    "revision": row["revision"],
                    "spec_version": spec.version,
                    "effective_from": effective.isoformat(),
                }
            )
        observation_limit = self._observation_limit(name, data, policy)
        end = observation_limit["end"]
        if sum(start <= d <= end for d in data.sessions) < 2:
            return None
        # Revision zero is the baseline, including when another study already
        # promoted a revision before this study's observation window began.
        if entries[0]["revision"] == 0:
            baseline = min(start, *(effective for effective, _ in schedule))
            schedule[0] = (baseline, schedule[0][1])
            entries[0]["effective_from"] = baseline.isoformat()
        schedule.sort(key=lambda item: item[0])
        entries.sort(key=lambda item: item["effective_from"])
        frozen = data.prefix(end)
        evaluator = CommonEvaluator(policy)
        result = evaluator.evaluate_schedule(frozen, schedule, start, end)
        cohorts = [
            {
                key: cohort[key]
                for key in ("cohort_id", "start", "end", "commitment_id", "sealed")
            }
            for cohort in self._cohorts(policy.market)
            if cohort["start"] <= end.isoformat() and cohort["end"] >= start.isoformat()
        ]
        exposure = self.db.execute(
            "SELECT 1 FROM agent_intervals WHERE market=? AND owner=? "
            "AND kind='observation_exposure' AND start<=? AND end>=?",
            (
                policy.market,
                self._observation_stream(policy.market),
                start.isoformat(),
                end.isoformat(),
            ),
        ).fetchone()
        report = {
            "kind": "CONTINUOUS_OBSERVATION_LEDGER",
            "status": "ok",
            "observation_limit": observation_limit,
            "cohorts": cohorts,
            "sealed": any(cohort["sealed"] for cohort in cohorts),
            "exposure_recorded": bool(exposure),
            "start": start,
            "end": end,
            "observations": len(result.daily_returns),
            "schedule": entries,
            "ledger": {
                "net_return": result.net_return,
                "max_drawdown": result.max_drawdown,
                "trades": result.trades,
                "fees": result.fees,
                "revision_boundaries": result.portfolio_diagnostics["schedule"][
                    "revision_boundaries"
                ],
            },
            "benchmarks": benchmark_report(
                evaluator, frozen, start, end, result, "observation"
            ),
            "persisted": False,
            "promotion_authority": False,
            "orders_submitted": 0,
            "proposer_visible": False,
        }
        return json.loads(canonical(report))

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
            if (
                report["deterioration_detected"]
                and report["observations"] == policy.shadow_sessions
            ):
                self._seal_memory(
                    name,
                    cycle["id"],
                    "observation",
                    "TRACKING_DEGRADED",
                    report["start"],
                    report["end"],
                    {
                        "research_revision": report["research_revision"],
                        "observations": report["observations"],
                        "net_advantage": report["net_advantage"],
                        "deterioration_detected": True,
                        "candidate_drawdown": report["candidate"]["max_drawdown"],
                        "candidate_return": report["candidate"]["net_return"],
                        "reference_return": report["reference"]["net_return"],
                    },
                )
        return report
