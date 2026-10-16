"""Evaluation-bound releases and durable observation slots. No broker writes."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .contracts import (
    KST,
    AgentError,
    Costs,
    D,
    StrategySpec,
    aware,
    canonical,
    dec,
    digest,
    implementation_id,
    timestamp,
)
from .data import MarketView, Snapshot
from .execution import OPEN_STATES, RiskPolicy
from .store import Store
from .strategy import decide


@dataclass(frozen=True)
class MonitorPolicy:
    max_daily_loss: D
    max_drawdown: D
    max_data_age_seconds: int

    def __post_init__(self):
        for field in ("max_daily_loss", "max_drawdown"):
            value = dec(getattr(self, field))
            if not D(0) < value < D(1):
                raise AgentError("Monitor loss limits must be in (0,1)")
            object.__setattr__(self, field, value)
        if type(self.max_data_age_seconds) is not int or self.max_data_age_seconds <= 0:
            raise AgentError("Monitor data age must be a positive integer")


def load_shadow_policy(path: Path) -> tuple[RiskPolicy, MonitorPolicy]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if set(raw) != {"risk", "monitor"}:
            raise ValueError
        risk = {**raw["risk"], "allowlist": tuple(raw["risk"]["allowlist"])}
        return RiskPolicy(**risk), MonitorPolicy(**raw["monitor"])
    except (KeyError, TypeError, ValueError):
        raise AgentError("Invalid shadow policy; require explicit risk and monitor sections") from None


def _spec(raw: dict) -> StrategySpec:
    return StrategySpec(**{**raw, "gross_weight": dec(raw["gross_weight"]),
                           "min_momentum": dec(raw["min_momentum"])})


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AgentError("Nonfinite or malformed evaluation evidence")
    return float(value)


def _evidence(store: Store, run_id: str, snapshot: Snapshot) -> tuple[dict, StrategySpec]:
    row = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise AgentError("Unknown stored evaluation; report-file imports are not accepted")
    report = json.loads(row[0])
    config, holdout = report["configuration"], report["holdout"]
    if "evaluation_binding" in config:
        raise AgentError("Selector-bound logic uses the logic study registry; it cannot use a legacy shadow release")
    if report["run_id"] != run_id or digest(config) != run_id or config["snapshot_id"] != snapshot.id:
        raise AgentError("Evaluation configuration or data hash mismatch")
    if report["verdict"] != "ELIGIBLE_FOR_PAPER_REVIEW" or not holdout["consumed"]:
        raise AgentError("Candidate did not pass the stored independent evaluation")
    champion = _spec(config["champion"])
    trials = [trial for trial in report["trials"] if trial["version"] == report["selected_version"]]
    if len(trials) != 1 or trials[0]["status"] != "COMPLETE":
        raise AgentError("Missing completed selected trial")
    selected = _spec(trials[0]["spec"])
    if selected.version != report["selected_version"] or champion.version != report["champion_version"]:
        raise AgentError("Evaluation was produced by a different implementation")
    if selected.version == champion.version or asdict(selected) not in [asdict(_spec(s)) for s in config["candidates"]]:
        raise AgentError("Selected candidate is outside the evaluated set")
    interval = holdout["mean_daily_difference_95pct_interval"]
    improvement = _number(holdout["candidate_net_return"]) - _number(holdout["champion_net_return"])
    if (len(interval) != 2 or _number(interval[0]) <= 0 or _number(interval[1]) < interval[0]
            or improvement < _number(config["min_improvement"])
            or not math.isclose(improvement, _number(holdout["net_improvement"]), abs_tol=1e-12)
            or not 0 <= _number(holdout["candidate_drawdown"]) <= _number(config["max_drawdown"])
            or _number(holdout["cost_stress_improvement"]) <= 0
            or type(holdout["observations"]) is not int or holdout["observations"] < 30
            or _number(trials[0]["worst_fold_drawdown"]) > _number(config["max_drawdown"])):
        raise AgentError("Stored evidence does not satisfy evaluation gates")
    for symbol in snapshot.symbols:
        if not store.db.execute("SELECT 1 FROM holdouts WHERE symbol=? AND start=? AND end=? AND run_id=?",
                                (symbol, holdout["start"], holdout["end"], run_id)).fetchone():
            raise AgentError("Consumed holdout claim is missing")
    return report, selected


def stage_release(store: Store, run_id: str, snapshot: Snapshot,
                  risk: RiskPolicy, monitor: MonitorPolicy) -> dict:
    """Store immutable review material; never change an execution strategy pointer."""
    try:
        report, selected = _evidence(store, run_id, snapshot)
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        if isinstance(exc, AgentError):
            raise
        raise AgentError("Malformed stored evaluation") from None
    if (set(snapshot.symbols) - set(risk.allowlist) or selected.gross_weight > risk.max_gross
            or selected.gross_weight / selected.top_n > risk.max_position):
        raise AgentError("Evaluated universe or allocation exceeds the shadow risk policy")
    # Validate the exact cost model bound to the evaluation, never a CLI override.
    costs = Costs(**report["configuration"]["costs"])
    manifest = {"schema": 1, "mode": "SHADOW_ONLY", "implementation_id": implementation_id(),
                "run_id": run_id, "evidence_hash": digest(report), "strategy": selected,
                "strategy_version": selected.version, "snapshot_id": snapshot.id,
                "evaluation_end": snapshot.sessions[-1], "universe": snapshot.symbols,
                "data_sources": sorted({b.source for b in snapshot.bars}),
                "costs": costs, "risk": risk, "monitor": monitor,
                "initial_evaluation_cash": report["configuration"]["capital"],
                "service_verification": "NOT_ESTABLISHED_BY_SCREENING", "live_enabled": False}
    manifest = json.loads(canonical(manifest))
    release_id = digest(manifest)
    with store.transaction():
        store.db.execute("INSERT OR IGNORE INTO releases VALUES(?,?)", (release_id, canonical(manifest)))
    return {"release_id": release_id, "manifest": manifest, "activated": False}


def get_release(store: Store, release_id: str) -> dict:
    row = store.db.execute("SELECT manifest_json FROM releases WHERE id=?", (release_id,)).fetchone()
    if not row:
        raise AgentError("Unknown release")
    manifest = json.loads(row[0])
    if digest(manifest) != release_id or manifest.get("mode") != "SHADOW_ONLY" or manifest.get("live_enabled") is not False:
        raise AgentError("Release manifest integrity failure")
    if manifest["implementation_id"] != implementation_id():
        raise AgentError("Release implementation differs from the running build")
    evidence = store.db.execute("SELECT report_json FROM experiments WHERE run_id=?", (manifest["run_id"],)).fetchone()
    if not evidence or digest(json.loads(evidence[0])) != manifest["evidence_hash"]:
        raise AgentError("Release evaluation evidence changed or disappeared")
    return manifest


def _label(value: str, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise AgentError(name + " must use 1..80 ASCII letters, digits, underscores or hyphens")


def activate_shadow(store: Store, slot: str, release_id: str, account: str,
                    expected_revision: int, reviewer: str, reason: str) -> dict:
    _label(slot, "Slot")
    if not reviewer.strip() or len(reviewer) > 120 or not reason.strip() or len(reason) > 500:
        raise AgentError("An explicit reviewer and review reason are required")
    if type(expected_revision) is not int or expected_revision < 0:
        raise AgentError("Expected revision must be a nonnegative integer")
    with store.transaction():
        get_release(store, release_id)
        store.wallet(account)
        if store.halt_reason(account):
            raise AgentError("Monitored account is halted")
        placeholders = ",".join("?" for _ in OPEN_STATES)
        if store.db.execute(f"SELECT 1 FROM orders WHERE account=? AND state IN ({placeholders})",
                            (account, *OPEN_STATES)).fetchone():
            raise AgentError("Resolve open orders before changing a shadow release")
        old = store.db.execute("SELECT * FROM shadow_slots WHERE slot=?", (slot,)).fetchone()
        if (old["revision"] if old else 0) != expected_revision:
            raise AgentError("Shadow revision conflict; reread the slot before reviewing")
        if old and (old["state"] == "STOPPED" or old["account"] != account):
            raise AgentError("A stopped slot or its account binding cannot be reset by activation")
        revision = expected_revision + 1
        store.db.execute("INSERT INTO shadow_slots VALUES(?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET revision=excluded.revision,release_id=excluded.release_id",
                         (slot, revision, release_id, account, "OBSERVING"))
        store.event(account, None, "SHADOW_RELEASE_REVIEWED", {"slot": slot, "revision": revision,
                    "release_id": release_id, "reviewer": reviewer, "reason": reason})
    return shadow_status(store, slot)


def shadow_status(store: Store, slot: str) -> dict:
    row = store.db.execute("SELECT * FROM shadow_slots WHERE slot=?", (slot,)).fetchone()
    if not row:
        raise AgentError("Unknown shadow slot")
    # Historical status remains readable across builds, but observe requires get_release().
    latest = store.db.execute("SELECT report_json FROM shadow_observations WHERE slot=? ORDER BY rowid DESC LIMIT 1", (slot,)).fetchone()
    return {"mode": "SHADOW_ONLY", **dict(row), "latest": json.loads(latest[0]) if latest else None,
            "orders_submitted": 0, "live_enabled": False}


def observe_shadow(store: Store, slot: str, snapshot: Snapshot, session: date, as_of: datetime) -> dict:
    at = aware(as_of)
    with store.transaction():
        status = shadow_status(store, slot)
        manifest = get_release(store, status["release_id"])
        cutoff = date.fromisoformat(manifest["evaluation_end"])
        history = [b for b in snapshot.bars if b.session <= cutoff]
        history_valid = (bool(history) and Snapshot(history).id == manifest["snapshot_id"]
                         and list(snapshot.symbols) == manifest["universe"])
        if session <= cutoff or session not in snapshot.sessions or at.astimezone(KST).date() != session:
            raise AgentError("Shadow requires a post-evaluation session on its observation date")
        policy = MonitorPolicy(**manifest["monitor"])
        risk = RiskPolicy(**manifest["risk"])
        wallet = store.wallet(status["account"])
        problems, view, decision, equity = [], None, None, None
        try:
            if not history_valid:
                problems.append("FROZEN_HISTORY_OR_UNIVERSE_CHANGED")
                raise AgentError("Invalid frozen data")
            raw_view = snapshot.view(at, session)
            view_id = digest(raw_view.histories)
            view = MarketView(view_id, at, session, raw_view.histories)
            if any((at - h[-1].available_at).total_seconds() > policy.max_data_age_seconds for h in view.histories.values()):
                problems.append("STALE_FINAL_BARS")
            if set(wallet.positions) - set(manifest["universe"]):
                problems.append("HOLDINGS_OUTSIDE_RELEASE_UNIVERSE")
            else:
                marks = {s: h[-1].close for s, h in view.histories.items()}
                equity = wallet.equity(marks)
                gross = sum((q * marks[s] for s, q in wallet.positions.items()), D(0))
                if equity <= 0:
                    problems.append("NONPOSITIVE_EQUITY")
                elif gross > equity * risk.max_gross or any(q * marks[s] > equity * risk.max_position for s, q in wallet.positions.items()):
                    problems.append("HOLDINGS_EXCEED_RISK_LIMITS")
            if any(len(h) <= manifest["strategy"]["lookback"] for h in view.histories.values()):
                problems.append("INSUFFICIENT_HISTORY")
            decision = decide(_spec(manifest["strategy"]), view,
                              at + timedelta(seconds=risk.max_quote_age_seconds))
        except AgentError:
            if history_valid:
                problems.append("UNAVAILABLE_OR_INVALID_FINAL_BARS")
        if store.halt_reason(status["account"]):
            problems.append("MONITORED_ACCOUNT_HALTED")
        if store.db.execute("SELECT 1 FROM orders WHERE account=? AND state IN ('SUBMITTING','UNKNOWN')", (status["account"],)).fetchone():
            problems.append("UNRESOLVED_SUBMISSION")
        inputs = {"view": view.snapshot_id if view else digest([b for b in snapshot.bars if b.session <= session]),
                  "wallet": wallet, "problems": problems, "account_halt": store.halt_reason(status["account"])}
        input_hash = digest(inputs)
        prior = store.db.execute("SELECT input_hash,report_json FROM shadow_observations WHERE slot=? AND revision=? AND as_of=?",
                                 (slot, status["revision"], at.isoformat())).fetchone()
        if prior:
            if prior[0] != input_hash:
                raise AgentError("Conflicting observation at an already recorded timestamp")
            return json.loads(prior[1])
        previous = status["latest"]
        if previous and at <= timestamp(previous["as_of"]):
            raise AgentError("Observation time must advance; historical rewrites are rejected")
        if status["state"] == "STOPPED":
            problems.append("STOP_LATCHED")
        day = session.isoformat()
        peak = dec(previous["peak_equity"]) if previous and previous["peak_equity"] is not None else equity
        baseline = (dec(previous["day_baseline_equity"]) if previous and previous["session"] == day
                    and previous["day_baseline_equity"] is not None else
                    dec(previous["equity"]) if previous and previous["equity"] is not None else equity)
        daily_loss, drawdown = None, None
        if equity is not None and peak is not None and baseline is not None:
            peak = max(peak, equity)
            daily_loss = max(D(0), 1 - equity / baseline) if baseline > 0 else D(1)
            drawdown = max(D(0), 1 - equity / peak) if peak > 0 else D(1)
            if daily_loss >= policy.max_daily_loss:
                problems.append("DAILY_LOSS_LIMIT")
            if drawdown >= policy.max_drawdown:
                problems.append("DRAWDOWN_LIMIT")
        stopped = bool(problems)
        report = {"slot": slot, "revision": status["revision"], "release_id": status["release_id"],
                  "session": day, "as_of": at, "state": "STOPPED" if stopped else "OBSERVING",
                  "problems": problems, "equity": equity, "peak_equity": peak,
                  "day_baseline_equity": baseline, "daily_loss": daily_loss, "drawdown": drawdown,
                  "decision": decision if not stopped else None, "input_hash": input_hash,
                  "ledger_basis": "LOCAL_JOURNAL_NOT_REFRESHED_FROM_BROKER",
                  "pnl_basis": "UNADJUSTED_FOR_EXTERNAL_CASH_FLOWS; STARTS_AT_FIRST_OBSERVATION",
                  "orders_submitted": 0, "live_enabled": False}
        report = json.loads(canonical(report))
        observation_id = digest({"slot": slot, "revision": status["revision"], "as_of": at})
        store.db.execute("INSERT INTO shadow_observations VALUES(?,?,?,?,?,?)",
                         (observation_id, slot, status["revision"], at.isoformat(), input_hash, canonical(report)))
        if stopped:
            store.db.execute("UPDATE shadow_slots SET state='STOPPED' WHERE slot=?", (slot,))
        store.event(status["account"], None, "SHADOW_OBSERVED", {"observation_id": observation_id, "state": report["state"]})
        return report
