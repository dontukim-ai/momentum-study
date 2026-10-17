"""Runnable research/paper vertical slice with zero runtime dependencies."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from .config import load_settings
from .contracts import UTC, AgentError, json_default, timestamp
from .data import read_csv, synthetic_snapshot, write_csv
from .evaluation import backtest
from .execution import OMS, RiskPolicy, SimulatedBroker
from .proposals import (
    StructuredProposer,
    development_context,
    generate_proposal,
    response_artifact,
)
from .research import BoundedLookbacks, research
from .runtime import paper_replay
from .store import Store


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=json_default, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="KIS agent: local screening/research/paper; no live order support")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("demo", "backtest", "research", "paper"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", type=Path)
        cmd.add_argument("--output", type=Path, default=Path("artifacts") / name)
        if name == "demo":
            cmd.add_argument("--sessions", type=int, default=220)
            cmd.add_argument("--seed", type=int, default=7)
        else:
            cmd.add_argument("--csv", type=Path, required=True)
        if name in {"research", "paper"}:
            cmd.add_argument("--db", type=Path, default=Path("artifacts/agent.sqlite3"))
        if name == "research":
            cmd.add_argument("--lookbacks", help="Comma-separated allowed lookbacks; only this parameter may change")
            proposal = cmd.add_mutually_exclusive_group()
            proposal.add_argument("--proposal-file", type=Path)
            proposal.add_argument("--llm", action="store_true", help="Request one audited structured LLM proposal")
            cmd.add_argument("--model")
        if name == "paper":
            cmd.add_argument("--account", default="paper-local")
            cmd.add_argument("--last-only", action="store_true")
    for name in ("status", "reconcile"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--db", type=Path, required=True)
        cmd.add_argument("--account", default="paper-local")
        cmd.add_argument("--config", type=Path)
    download = sub.add_parser("kis-download", help="Read-only KIS daily prices, never orders")
    download.add_argument("--symbols", required=True, help="Comma-separated six-digit codes")
    download.add_argument("--start", type=date.fromisoformat, required=True)
    download.add_argument("--end", type=date.fromisoformat, required=True)
    download.add_argument("--environment", choices=("paper", "real-readonly"), default="paper")
    download.add_argument("--historical-availability-assumption", action="store_true",
                          help="Explicitly assume historical final bars were available at 15:40 KST; not observed PIT data")
    download.add_argument("--output", type=Path, required=True)
    propose = sub.add_parser("propose", help="Generate a structured proposal from development-only context")
    propose.add_argument("--csv", type=Path, required=True)
    propose.add_argument("--db", type=Path, default=Path("artifacts/agent.sqlite3"))
    propose.add_argument("--config", type=Path)
    propose.add_argument("--model")
    propose.add_argument("--budget", type=int, default=8)
    propose.add_argument("--max-attempts", type=int, default=1)
    propose.add_argument("--recorded-response", type=Path, help="Offline response fixture; never calls a provider")
    propose.add_argument("--output", type=Path, required=True)
    paper = sub.add_parser("kis-paper", help="Explicit paper-host-only account/order operations")
    paper.add_argument("action", choices=("prepare", "init", "status", "quote", "orders", "submit", "cancel", "reconcile", "adopt", "resume"))
    paper.add_argument("--db", type=Path, default=Path("artifacts/kis-paper.sqlite3"))
    paper.add_argument("--account", default="kis-paper")
    paper.add_argument("--policy", type=Path)
    paper.add_argument("--symbol")
    paper.add_argument("--side", choices=("BUY", "SELL"))
    paper.add_argument("--quantity", type=int)
    paper.add_argument("--price")
    paper.add_argument("--ttl", type=int, default=120)
    paper.add_argument("--output", type=Path)
    paper.add_argument("--intent", type=Path)
    paper.add_argument("--order-key")
    paper.add_argument("--day", type=date.fromisoformat)
    paper.add_argument("--orgno")
    paper.add_argument("--odno")
    doctor = sub.add_parser("doctor", help="Secret-free readiness; opt-in read-only KIS real/paper probe")
    probes = doctor.add_mutually_exclusive_group()
    probes.add_argument("--probe-paper", action="store_true")
    probes.add_argument("--probe-real", action="store_true")
    doctor.add_argument("--symbol")
    doctor.add_argument("--token-cache-dir", type=Path, default=Path("artifacts/.secrets"))
    doctor.add_argument("--output", type=Path)
    release = sub.add_parser("release", help="Stage an evaluation-bound shadow-only release")
    release.add_argument("--db", type=Path, required=True)
    release.add_argument("--run-id", required=True)
    release.add_argument("--csv", type=Path, required=True)
    release.add_argument("--policy", type=Path, required=True)
    release.add_argument("--output", type=Path)
    shadow = sub.add_parser("shadow", help="Review, observe or inspect an order-free shadow slot")
    shadow.add_argument("action", choices=("activate", "observe", "status"))
    shadow.add_argument("--db", type=Path, required=True)
    shadow.add_argument("--slot", required=True)
    shadow.add_argument("--release-id")
    shadow.add_argument("--account")
    shadow.add_argument("--expected-revision", type=int)
    shadow.add_argument("--reviewer")
    shadow.add_argument("--reason")
    shadow.add_argument("--csv", type=Path)
    shadow.add_argument("--session", type=date.fromisoformat)
    shadow.add_argument("--as-of", type=timestamp)
    shadow.add_argument("--output", type=Path)
    logic = sub.add_parser("logic", help="Order-free security screening and automatic strategy improvement")
    logic.add_argument("action", choices=("init", "run", "screen", "status", "recover", "abandon"))
    logic.add_argument("--db", type=Path, required=True)
    logic.add_argument("--study", default="main")
    logic.add_argument("--config", type=Path)
    logic.add_argument("--csv", type=Path)
    logic.add_argument("--securities", type=Path)
    logic.add_argument("--session", type=date.fromisoformat)
    logic.add_argument("--as-of", type=timestamp)
    proposal = logic.add_mutually_exclusive_group()
    proposal.add_argument("--proposal-file", type=Path)
    proposal.add_argument("--llm", action="store_true")
    logic.add_argument("--model")
    logic.add_argument("--cycle-id")
    logic.add_argument("--reason")
    logic.add_argument("--output", type=Path, default=Path("artifacts/logic"))
    return root


def run(args: argparse.Namespace) -> dict:
    if args.command == "logic":
        from .logic_cli import run_logic_command
        return run_logic_command(args)
    if args.command == "doctor":
        from .diagnostics import doctor
        result = doctor(probe_paper=args.probe_paper, probe_real=args.probe_real,
                        symbol=args.symbol, token_cache_dir=args.token_cache_dir)
        if args.output:
            save_json(args.output, result)
        return result
    if args.command in {"release", "shadow"}:
        from datetime import datetime

        from .releases import (
            activate_shadow,
            load_shadow_policy,
            observe_shadow,
            shadow_status,
            stage_release,
        )
        if not args.db.exists():
            raise AgentError("Database does not exist; use the original evaluation database")
        store = Store(args.db)
        try:
            if args.command == "release":
                risk, monitor = load_shadow_policy(args.policy)
                result = stage_release(store, args.run_id, read_csv(args.csv), risk, monitor)
            elif args.action == "activate":
                if not args.release_id or not args.account or args.expected_revision is None or not args.reviewer or not args.reason:
                    raise AgentError("Activation requires release-id, account, expected-revision, reviewer and reason")
                result = activate_shadow(store, args.slot, args.release_id, args.account,
                                         args.expected_revision, args.reviewer, args.reason)
            elif args.action == "observe":
                if not args.csv or not args.session:
                    raise AgentError("Observation requires --csv and --session")
                result = observe_shadow(store, args.slot, read_csv(args.csv), args.session, args.as_of or datetime.now(UTC))
            else:
                result = shadow_status(store, args.slot)
            if args.output:
                save_json(args.output, result)
            return result
        finally:
            store.close()
    if args.command == "kis-paper":
        from .paper_cli import run_paper_command
        return run_paper_command(args)
    if args.command == "kis-download":
        from .kis import KisReadOnly
        snapshot = KisReadOnly(args.environment).collect(args.symbols.split(","), args.start, args.end,
                                                        args.historical_availability_assumption)
        write_csv(snapshot, args.output)
        return {"mode": "KIS_READ_ONLY", "rows": len(snapshot.bars), "snapshot_id": snapshot.id,
                "availability": "ASSUMED_HISTORICAL" if args.historical_availability_assumption else "OBSERVED_AT_COLLECTION",
                "output": str(args.output.resolve())}
    settings = load_settings(args.config)
    if args.command == "propose":
        context = development_context(read_csv(args.csv), settings.strategy)
        store = Store(args.db)
        try:
            if args.recorded_response:
                if args.recorded_response.stat().st_size > 262144:
                    raise AgentError("Recorded response exceeds size limit")
                try:
                    response = json.loads(args.recorded_response.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    raise AgentError("Invalid recorded-response JSON") from None
                artifact = response_artifact(context, args.model or "offline-fixture", response, args.budget)
            else:
                artifact = generate_proposal(context, store, args.model, args.budget, max_attempts=args.max_attempts)
            save_json(args.output, artifact)
            return {"context_hash": artifact["context_hash"], "provider": artifact["provenance"]["provider"],
                    "proposal": artifact["proposal"], "output": str(args.output.resolve())}
        finally:
            store.close()
    if args.command in {"status", "reconcile"}:
        if not args.db.exists():
            raise AgentError("Database does not exist")
        store = Store(args.db)
        try:
            if args.command == "status":
                return store.status(args.account)
            binding = store.db.execute("SELECT mode FROM broker_bindings WHERE account=?", (args.account,)).fetchone()
            if binding and binding[0] == "KIS_PAPER":
                raise AgentError("Use kis-paper reconcile for a KIS-bound account")
            symbols = tuple(r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM orders WHERE account=?", (args.account,))) or ("UNUSED",)
            return OMS(store, SimulatedBroker(store, settings.costs), RiskPolicy(symbols), settings.costs).reconcile(args.account)
        finally:
            store.close()
    snapshot = synthetic_snapshot(args.sessions, args.seed) if args.command == "demo" else read_csv(args.csv)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.command == "backtest":
        result = backtest(snapshot, settings.strategy, settings.costs, settings.initial_cash)
        save_json(args.output / "backtest.json", result)
        return {"mode": "DAILY_OPEN_SCREENING", "net_return": result.net_return,
                "trades": result.trades, "report": str((args.output / "backtest.json").resolve())}
    db = args.output / "agent.sqlite3" if args.command == "demo" else args.db
    store = Store(db)
    try:
        if args.command == "paper":
            result = paper_replay(snapshot, settings.strategy, settings.costs, store, args.account,
                                  settings.initial_cash, args.last_only)
            save_json(args.output / "paper.json", result)
            return {**result, "report": str((args.output / "paper.json").resolve())}
        lookbacks = settings.lookbacks
        if args.command == "research" and args.lookbacks:
            try:
                lookbacks = tuple(int(x) for x in args.lookbacks.split(","))
            except ValueError:
                raise AgentError("lookbacks must be comma-separated integers") from None
        proposer = BoundedLookbacks(lookbacks)
        if args.command == "research" and (args.proposal_file or args.llm):
            if args.lookbacks:
                raise AgentError("Choose explicit lookbacks or structured proposals, not both")
            context = development_context(snapshot, settings.strategy)
            if args.proposal_file:
                proposer = StructuredProposer.from_file(args.proposal_file, context)
            else:
                artifact = generate_proposal(context, store, args.model)
                save_json(args.output / "proposal.json", artifact)
                proposer = StructuredProposer(artifact, context)
        result = research(snapshot, settings.strategy, settings.costs, store, proposer, settings.initial_cash)
        save_json(args.output / "research.json", result)
        if args.command == "research":
            return {"verdict": result["verdict"], "run_id": result["run_id"], "cached_report": result["cached_report"],
                    "report": str((args.output / "research.json").resolve())}
        write_csv(snapshot, args.output / "synthetic.csv")
        baseline = backtest(snapshot, settings.strategy, settings.costs, settings.initial_cash)
        paper = paper_replay(snapshot, settings.strategy, settings.costs, store, "demo", settings.initial_cash)
        save_json(args.output / "backtest.json", baseline)
        save_json(args.output / "paper.json", paper)
        report_path = args.output / "report.md"
        report_path.write_text(
            "**Synthetic local demonstration — not market performance**\n\n"
            f"Data: {len(snapshot.sessions)} synthetic weekday sessions, {len(snapshot.symbols)} symbols.\n\n"
            "| Item | Result |\n| --- | --- |\n"
            f"| Snapshot | {snapshot.id} |\n"
            f"| Screening net return | {baseline.net_return:.4%} |\n"
            f"| Screening max drawdown | {baseline.max_drawdown:.4%} |\n"
            f"| Screening fills | {baseline.trades} |\n"
            f"| Research verdict | {result['verdict']} |\n"
            f"| Holdout consumed | {result['holdout']['consumed']} |\n"
            f"| Local paper equity | {paper['final_equity']} |\n"
            f"| Reconciliation matches | {paper['reconciliation']['matches']} |\n"
            f"| Unresolved submissions | {len(paper['reconciliation']['unresolved'])} |\n\n"
            "The runtime keeps the champion. A research verdict never activates a strategy or sends KIS orders.\n\n"
            "Daily-open full-fill approximations and example fees require intraday/forward validation. "
            "Corporate actions, real exchange calendars, live drawdown breakers, Lean and PostgreSQL are not integrated yet.\n",
            encoding="utf-8")
        return {"mode": "SYNTHETIC_LOCAL_DEMO", "snapshot_id": snapshot.id,
                "screening_net_return": baseline.net_return, "research_verdict": result["verdict"],
                "paper_reconciliation": paper["reconciliation"], "paper_orders": paper["orders"],
                "paper_rejections": len(paper["rejections"]), "report": str(report_path.resolve()), "database": str(db.resolve())}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = run(args)
    except (AgentError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, default=json_default, indent=2, ensure_ascii=False, allow_nan=False))
    return 0
