"""Order-free automatic research: screen, propose, independently evaluate, remember."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path

from .contracts import (
    AgentError,
    Costs,
    D,
    canonical,
    digest,
    implementation_id,
    session_time,
)
from .data import Snapshot
from .evaluation import backtest
from .proposals import StructuredProposer, generate_proposal
from .research import research
from .selection import (
    DEFAULT_RANKINGS,
    LOOKBACK_FREE_RANKINGS,
    RANKINGS,
    RankedStrategy,
    SecurityHistory,
    SelectionEngine,
    SelectionPolicy,
    ranked_spec,
    select_candidates,
)
from .store import Store


@dataclass(frozen=True)
class EvaluationPolicy:
    holdout_fraction: float = .2
    min_improvement: float = .001
    max_drawdown: float = .2
    min_fold_trades: int = 2
    min_holdout_trades: int = 4

    def __post_init__(self):
        for name in ("holdout_fraction", "min_improvement", "max_drawdown"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise AgentError("Evaluation thresholds must be finite numbers")
        if not .1 <= self.holdout_fraction <= .4 or self.min_improvement < 0 or not 0 < self.max_drawdown < 1:
            raise AgentError("Invalid fixed evaluation thresholds")
        if any(type(getattr(self, n)) is not int or getattr(self, n) < 1 for n in ("min_fold_trades", "min_holdout_trades")):
            raise AgentError("Positive fold and holdout activity requirements are required")


def candidate_grid(champion: RankedStrategy, lookbacks, rankings) -> list[RankedStrategy]:
    """Lookback x ranking challengers, champion excluded. Fundamental and trend
    rankings ignore the lookback, so they are proposed once, at the champion's."""
    grid = [replace(champion, lookback=n, ranking=r) for n in lookbacks for r in rankings if r not in LOOKBACK_FREE_RANKINGS]
    grid += [replace(champion, ranking=r) for r in rankings if r in LOOKBACK_FREE_RANKINGS]
    unique = {spec.version: spec for spec in grid}
    unique.pop(champion.version, None)
    return list(unique.values())


@dataclass(frozen=True)
class LogicSettings:
    champion: RankedStrategy = field(default_factory=RankedStrategy)
    selection: SelectionPolicy = field(default_factory=SelectionPolicy)
    costs: Costs = field(default_factory=lambda: Costs(D("1.5"), D("20"), D("5")))
    evaluation: EvaluationPolicy = field(default_factory=EvaluationPolicy)
    lookbacks: tuple[int, ...] = (5, 10, 20)
    rankings: tuple[str, ...] = DEFAULT_RANKINGS
    candidate_budget: int = 8

    def __post_init__(self):
        object.__setattr__(self, "lookbacks", tuple(self.lookbacks))
        object.__setattr__(self, "rankings", tuple(self.rankings))
        if (not self.lookbacks or len(set(self.lookbacks)) != len(self.lookbacks)
                or any(type(n) is not int or not 2 <= n <= 60 for n in self.lookbacks)
                or not self.rankings or len(set(self.rankings)) != len(self.rankings) or set(self.rankings) - set(RANKINGS)):
            raise AgentError("Invalid preregistered ranking/search space")
        if type(self.candidate_budget) is not int or not 1 <= self.candidate_budget <= 12:
            raise AgentError("Logic challenger budget must be 1..12")
        if len(candidate_grid(self.champion, self.lookbacks, self.rankings)) > self.candidate_budget:
            raise AgentError("Preregistered search space exceeds the challenger budget")
        if self.champion.gross_weight > self.selection.max_gross or self.champion.gross_weight / self.champion.top_n > self.selection.max_position:
            raise AgentError("Champion allocation exceeds the fixed policy")


def settings_from_dict(raw: dict) -> LogicSettings:
    try:
        values = dict(raw)
        constructors = {"champion": RankedStrategy, "selection": SelectionPolicy, "costs": Costs, "evaluation": EvaluationPolicy}
        for name, constructor in constructors.items():
            if name in values:
                values[name] = constructor(**values[name])
        return LogicSettings(**values)
    except (TypeError, ValueError, AttributeError):
        raise AgentError("Invalid logic settings; unknown keys and policy changes are rejected") from None


def load_logic_settings(path: Path | None = None) -> LogicSettings:
    if path is None:
        return LogicSettings()
    if path.stat().st_size > 65536:
        raise AgentError("Logic configuration exceeds size budget")
    try:
        return settings_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, UnicodeError):
        raise AgentError("Invalid logic settings JSON") from None


def logic_development_context(snapshot: Snapshot, securities: SecurityHistory, champion: RankedStrategy,
                              settings: LogicSettings) -> dict:
    boundary = len(snapshot.sessions) - max(30, int(len(snapshot.sessions) * settings.evaluation.holdout_fraction))
    if boundary - max(61, settings.selection.min_history) < 45:
        raise AgentError("Need at least 45 development sessions after fixed warmup, and 30 holdout sessions")
    cutoff = snapshot.sessions[boundary - 1]
    at = session_time(cutoff, 16)
    view = snapshot.view(at, cutoff, allow_missing=True)
    report = select_candidates(champion, view, securities, settings.selection, settings.costs, at + timedelta(hours=1))
    rows = sorted(report["candidates"], key=lambda r: (r["status"] == "REJECTED", r["symbol"]))
    reasons = {}
    for row in rows:
        for reason in row["reasons"]:
            reasons[reason] = reasons.get(reason, 0) + 1
    return {"purpose": "development-only screened logic proposals; no order authority",
            "as_of": at, "development_end": cutoff, "visible_input_id": report["visible_input_id"],
            "champion": champion, "champion_version": champion.version,
            "screened_assets": [{"symbol": r["symbol"], "features": r["features"], "reasons": r["reasons"]} for r in rows[:20]],
            "excluded_reason_counts": reasons, "policy_hash": digest(settings.selection),
            "allowed_change": {"lookback": {"minimum": 2, "maximum": 60},
                               "rankings": settings.rankings, "challenger_budget": settings.candidate_budget},
            "restrictions": "No code, allocation, universe policy, costs or evaluation changes; no holdout data provided"}


class LogicProposer:
    def __init__(self, settings: LogicSettings, *, artifact: dict | None = None, context: dict | None = None):
        self.settings, self.structured = settings, None
        self.metadata = {"kind": "bounded-ranking-grid", "hypothesis": "Momentum horizon and volatility normalization may improve costed consistency",
                         "falsification": "Reject if chronological folds, independent holdout, activity, cost stress or confidence gate fails"}
        if artifact is not None:
            self.structured = StructuredProposer(artifact, context, max(1, settings.candidate_budget // len(settings.rankings)))
            self.metadata = {**self.metadata, "structured": self.structured.metadata}

    def propose(self, champion):
        lookbacks = self.structured.bundle["lookbacks"] if self.structured else self.settings.lookbacks
        candidates = {spec.version: spec for spec in candidate_grid(champion, lookbacks, self.settings.rankings)}
        if len(candidates) > self.settings.candidate_budget:
            if self.structured:
                raise AgentError("Logic proposal exceeds the fixed challenger budget")
            candidates = dict(list(candidates.items())[:self.settings.candidate_budget])
        return list(candidates.values()) or [champion]


def _allowed(spec, champion):
    return (type(spec) is RankedStrategy and replace(spec, lookback=champion.lookback, ranking=champion.ranking) == champion
            and spec.ranking in RANKINGS)


def _validate_promotion(store, report, settings, inputs):
    """Recheck stored gates when recovering; never trust an edited verdict alone."""
    if report["verdict"] not in {"KEEP_CHAMPION", "ELIGIBLE_FOR_PAPER_REVIEW"}:
        raise AgentError("Unknown logic evaluation verdict")
    config, policy = report["configuration"], settings.evaluation
    if (config["costs"] != json.loads(canonical(settings.costs))
            or config["capital"] != str(settings.selection.capital_limit)
            or any(config[name] != getattr(policy, name) for name in ("holdout_fraction", "min_improvement", "max_drawdown"))
            or config["activity_requirements"] != {"min_fold_trades": policy.min_fold_trades, "min_holdout_trades": policy.min_holdout_trades}
            or config["evaluation_binding"]["implementation_id"] != implementation_id()):
        raise AgentError("Stored evaluation changed the fixed research contract")
    if report["verdict"] == "KEEP_CHAMPION":
        return
    holdout = report["holdout"]
    selected = [t for t in report["trials"] if t["version"] == report["selected_version"]]
    base = [t for t in report["trials"] if t["version"] == report["champion_version"]]
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise AgentError("Malformed numeric evaluation evidence")
        return value
    interval = holdout["mean_daily_difference_95pct_interval"]
    improvement = number(holdout["candidate_net_return"]) - number(holdout["champion_net_return"])
    if (len(selected) != 1 or len(base) != 1 or selected[0]["status"] != "COMPLETE" or base[0]["status"] != "COMPLETE"
            or report["selected_version"] == report["champion_version"]
            or number(selected[0]["dev_mean_daily_return"]) <= number(base[0]["dev_mean_daily_return"])
            or any(f["trades"] < policy.min_fold_trades for f in selected[0]["folds"])
            or number(selected[0]["worst_fold_drawdown"]) > policy.max_drawdown
            or not holdout["consumed"] or improvement < policy.min_improvement
            or not math.isclose(improvement, number(holdout["net_improvement"]), abs_tol=1e-12)
            or len(interval) != 2 or number(interval[0]) <= 0 or number(interval[1]) < interval[0]
            or not 0 <= number(holdout["candidate_drawdown"]) <= policy.max_drawdown
            or number(holdout["cost_stress_improvement"]) <= 0
            or type(holdout["observations"]) is not int or holdout["observations"] < 30
            or type(holdout["candidate_trades"]) is not int or holdout["candidate_trades"] < policy.min_holdout_trades):
        raise AgentError("Stored evaluation does not satisfy the independent promotion gates")
    for symbol in inputs["symbols"]:
        if not store.db.execute("SELECT 1 FROM holdouts WHERE symbol=? AND start=? AND end=? AND run_id=?",
                                (symbol, holdout["start"], holdout["end"], report["run_id"])).fetchone():
            raise AgentError("Independent holdout claim is missing")


def evaluate_logic(snapshot: Snapshot, securities: SecurityHistory, champion: RankedStrategy,
                   settings: LogicSettings, store: Store, proposer: LogicProposer, on_registered=None):
    expected = logic_development_context(snapshot, securities, champion, settings)
    if proposer.settings != settings or (proposer.structured and proposer.structured.metadata["context_hash"] != digest(expected)):
        raise AgentError("Logic proposal does not match the evaluator's development context or fixed policy")
    binding = {"engine": "point-in-time-selection-v1", "security_history_id": securities.id,
               "selection_policy": settings.selection, "implementation_id": implementation_id()}

    def evaluate(data, spec, costs, cash, start, end):
        if data.id != snapshot.id or cash != settings.selection.capital_limit:
            raise AgentError("Logic evaluator input binding mismatch")
        engine = SelectionEngine(securities, settings.selection, costs)
        return backtest(data, spec, costs, cash, start, end,
                        decision_engine=engine, execution_filter=engine.execution_allowed,
                        evaluation_binding=binding, risk_policy=settings.selection.risk(data.symbols),
                        capital_limit=settings.selection.capital_limit)

    policy = settings.evaluation
    return research(snapshot, champion, settings.costs, store, proposer, settings.selection.capital_limit,
                    policy.holdout_fraction, policy.min_improvement, policy.max_drawdown,
                    evaluator=evaluate, candidate_validator=lambda spec, base: _allowed(spec, base) and spec.ranking in settings.rankings,
                    evaluation_binding=binding,
                    minimum_warmup=max(61, settings.selection.min_history), min_fold_trades=policy.min_fold_trades,
                    min_holdout_trades=policy.min_holdout_trades, on_registered=on_registered)


def init_study(store: Store, name: str, settings: LogicSettings) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise AgentError("Study name must use 1..80 ASCII letters, digits, underscores or hyphens")
    with store.transaction():
        row = store.db.execute("SELECT * FROM logic_studies WHERE name=?", (name,)).fetchone()
        if row and (row["settings_json"] != canonical(settings) or row["implementation_id"] != implementation_id()):
            raise AgentError("Study is bound to its policy, search space and build; preserve its original history")
        store.db.execute("INSERT OR IGNORE INTO logic_studies VALUES(?,?,?,?,?)",
                         (name, 0, canonical(settings), canonical(settings.champion), implementation_id()))
    return study_status(store, name)


def study_status(store: Store, name: str) -> dict:
    row = store.db.execute("SELECT * FROM logic_studies WHERE name=?", (name,)).fetchone()
    if not row:
        raise AgentError("Unknown logic study")
    return {"mode": "AUTOMATED_RESEARCH_ONLY", "name": name, "revision": row["revision"],
            "champion": json.loads(row["champion_json"]), "implementation_id": row["implementation_id"],
            "settings": json.loads(row["settings_json"]),
            "cycles": [dict(r) for r in store.db.execute("SELECT id,status,expected_revision,run_id FROM logic_cycles WHERE study=? ORDER BY rowid", (name,))],
            "orders_submitted": 0, "live_enabled": False}


def _complete_cycle(store: Store, cycle_id: str) -> dict:
    with store.transaction():
        cycle = store.db.execute("SELECT * FROM logic_cycles WHERE id=?", (cycle_id,)).fetchone()
        if not cycle:
            raise AgentError("Unknown logic cycle")
        if cycle["status"] == "COMPLETE":
            return _cached_cycle(store, cycle)
        if cycle["status"] != "RUNNING" or not cycle["run_id"]:
            raise AgentError("Cycle has no completed evaluation to recover")
        row = store.db.execute("SELECT * FROM logic_studies WHERE name=?", (cycle["study"],)).fetchone()
        experiment = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (cycle["run_id"],)).fetchone()
        if not experiment:
            raise AgentError("Evaluation is incomplete; it will not be rerun against a consumed holdout")
        report, inputs = json.loads(experiment[0]), json.loads(cycle["input_json"])
        config = report["configuration"]
        if (digest(inputs) != cycle_id or digest(json.loads(row["settings_json"])) != inputs["settings_hash"]
                or row["implementation_id"] != implementation_id() or row["revision"] != cycle["expected_revision"]
                or digest(config) != cycle["run_id"] or report["run_id"] != cycle["run_id"]
                or config["snapshot_id"] != inputs["snapshot_id"]
                or config["evaluation_binding"]["security_history_id"] != inputs["security_history_id"]
                or config["evaluation_binding"]["selection_policy"] != json.loads(row["settings_json"])["selection"]
                or ranked_spec(config["champion"]).version != ranked_spec(json.loads(row["champion_json"])).version):
            raise AgentError("Cycle evaluation, policy, build or champion revision mismatch")
        _validate_promotion(store, report, settings_from_dict(json.loads(row["settings_json"])), inputs)
        promoted = report["verdict"] == "ELIGIBLE_FOR_PAPER_REVIEW"
        champion_before = ranked_spec(json.loads(row["champion_json"]))
        champion_after = champion_before
        if promoted:
            selected = [t for t in report["trials"] if t["version"] == report["selected_version"] and t["status"] == "COMPLETE"]
            if len(selected) != 1 or not report["holdout"]["consumed"]:
                raise AgentError("Missing selected candidate or independent evidence")
            champion_after = ranked_spec(selected[0]["spec"])
            if (not _allowed(champion_after, champion_before) or champion_after.version != report["selected_version"]
                    or champion_after.ranking not in settings_from_dict(json.loads(row["settings_json"])).rankings):
                raise AgentError("Selected research strategy violated the allowed change boundary")
            # Only the research champion changes. No execution/shadow pointer or broker is called.
            store.db.execute("UPDATE logic_studies SET revision=revision+1,champion_json=? WHERE name=?",
                             (canonical(champion_after), cycle["study"]))
        result = {"cycle_id": cycle_id, "study": cycle["study"], "mode": "AUTOMATED_RESEARCH_ONLY",
                  "run_id": cycle["run_id"], "evidence_hash": digest(report),
                  "revision_before": row["revision"], "revision_after": row["revision"] + int(promoted),
                  "champion_before": champion_before, "champion_after": champion_after,
                  "research_champion_updated": promoted, "verdict": report["verdict"],
                  "trials": report["total_trials"], "holdout": report["holdout"],
                  "orders_submitted": 0, "live_enabled": False}
        store.db.execute("UPDATE logic_cycles SET status='COMPLETE',report_json=? WHERE id=?", (canonical(result), cycle_id))
        store.event("logic:" + cycle["study"], None, "LOGIC_CYCLE_COMPLETED", result)
    return json.loads(canonical(result))


def recover_cycle(store: Store, cycle_id: str) -> dict:
    """Consume an already stored evaluation only. Never invoke proposal/evaluation again."""
    return _complete_cycle(store, cycle_id)


def _cached_cycle(store, cycle):
    result = json.loads(cycle["report_json"])
    evidence = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (cycle["run_id"],)).fetchone()
    if (not evidence or digest(json.loads(evidence[0])) != result["evidence_hash"]
            or result["cycle_id"] != cycle["id"] or result["run_id"] != cycle["run_id"]):
        raise AgentError("Completed cycle evidence changed or disappeared")
    return result


def abandon_cycle(store: Store, cycle_id: str, reason: str) -> dict:
    """Operator retirement after stopping the worker; all consumed intervals remain."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise AgentError("An explicit abandonment reason up to 500 characters is required")
    with store.transaction():
        cycle = store.db.execute("SELECT * FROM logic_cycles WHERE id=?", (cycle_id,)).fetchone()
        if not cycle or cycle["status"] != "RUNNING":
            raise AgentError("Only an interrupted running cycle can be abandoned")
        if cycle["run_id"] and store.db.execute("SELECT 1 FROM experiments WHERE run_id=?", (cycle["run_id"],)).fetchone():
            raise AgentError("Completed evaluation exists; recover its evidence rather than discard it")
        result = {"cycle_id": cycle_id, "status": "ABANDONED", "reason": reason,
                  "holdout_claims_preserved": True, "champion_changed": False, "orders_submitted": 0}
        store.db.execute("UPDATE logic_cycles SET status='ABANDONED',report_json=? WHERE id=?", (canonical(result),cycle_id))
        store.event("logic:" + cycle["study"], None, "LOGIC_CYCLE_ABANDONED", result)
    return result


def run_logic_cycle(store: Store, name: str, snapshot: Snapshot, securities: SecurityHistory,
                    *, proposal_artifact: dict | None = None, llm: bool = False, model: str | None = None) -> dict:
    if proposal_artifact is not None and llm:
        raise AgentError("Choose a proposal artifact or LLM, not both")
    status = study_status(store, name)
    if status["implementation_id"] != implementation_id():
        raise AgentError("Study build changed; retain history and review a forward-data migration")
    settings = settings_from_dict(status["settings"])
    inputs = {"study": name, "snapshot_id": snapshot.id, "security_history_id": securities.id,
              "build": implementation_id(), "settings_hash": digest(settings), "symbols": snapshot.symbols,
              "end_session": snapshot.sessions[-1].isoformat(),
              "security_prefix_id": digest(securities.visible(session_time(snapshot.sessions[-1], 16)))}
    cycle_id = digest(inputs)
    with store.transaction():
        prior = store.db.execute("SELECT * FROM logic_cycles WHERE id=?", (cycle_id,)).fetchone()
        if prior:
            if prior["status"] == "COMPLETE":
                result = _cached_cycle(store, prior)
                if proposal_artifact is not None or llm:
                    raise AgentError("Cycle already completed; use stored evidence rather than reproposing on the same data")
                return {**result, "cached_cycle": True}
            raise AgentError("Existing cycle is running, interrupted or failed; inspect it and recover stored evidence only")
        current = store.db.execute("SELECT revision FROM logic_studies WHERE name=?", (name,)).fetchone()[0]
        if current != status["revision"] or store.db.execute("SELECT 1 FROM logic_cycles WHERE study=? AND status='RUNNING'", (name,)).fetchone():
            raise AgentError("Another cycle changed or owns this study")
        previous = store.db.execute("SELECT * FROM logic_cycles WHERE study=? AND status IN ('COMPLETE','ABANDONED','FAILED') ORDER BY rowid DESC LIMIT 1", (name,)).fetchone()
        if previous:
            if previous["status"] == "COMPLETE":
                _cached_cycle(store, previous)
            from datetime import date
            frozen = json.loads(previous["input_json"])
            end = date.fromisoformat(frozen["end_session"])
            if snapshot.sessions[-1] <= end:
                raise AgentError("A new cycle requires new forward sessions, not changed inputs on the same history")
            prefix = [b for b in snapshot.bars if b.session <= end]
            if (not prefix or Snapshot(prefix, require_complete=False).id != frozen["snapshot_id"]
                    or digest(securities.visible(session_time(end, 16))) != frozen["security_prefix_id"]):
                raise AgentError("Forward cycle changed frozen market or security history")
        store.db.execute("INSERT INTO logic_cycles VALUES(?,?,?,?,?,?,?)",
                         (cycle_id, name, current, "RUNNING", canonical(inputs), None, None))
    try:
        champion = ranked_spec(status["champion"])
        context = logic_development_context(snapshot, securities, champion, settings)
        if llm:
            budget = max(1, settings.candidate_budget // len(settings.rankings))
            proposal_artifact = generate_proposal(context, store, model, budget)
        proposer = LogicProposer(settings, artifact=proposal_artifact, context=context)

        def registered(run_id):
            store.db.execute("UPDATE logic_cycles SET run_id=? WHERE id=? AND status='RUNNING'", (run_id, cycle_id))

        evaluate_logic(snapshot, securities, champion, settings, store, proposer, registered)
    except Exception:
        # A completed evaluation can be recovered explicitly after a process fault.
        # Failures never rewrite the champion or delete claimed holdout intervals.
        with store.transaction():
            store.db.execute("UPDATE logic_cycles SET status='FAILED',report_json=? WHERE id=? AND status='RUNNING'",
                             (canonical({"error_category": "proposal_or_evaluation_failure", "orders_submitted": 0}), cycle_id))
        raise
    return _complete_cycle(store, cycle_id)
