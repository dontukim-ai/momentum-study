"""Run the four-stage research agent once or continuously on immutable inputs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, canonical, timestamp
from trading_agent.data import read_csv
from trading_agent.selection import read_security_history
from trading_agent.store import Store

from momentum.ledger.ledger import CorporateAction

from .bundle import open_bundle, publish_bundle
from .contracts import AgentPolicy, AnnouncedAction, ResearchInput, policy_from_dict
from .quality import assess, screen_price_rows
from .service import ResearchAgent


def save_json(path: Path, value: Any) -> None:
    """Readers see either the old result or the entire new result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(canonical(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_input(
    prices: Path,
    securities: Path,
    actions: Path | None = None,
    sessions: Path | None = None,
    basis: str = "raw-unadjusted; supplied-actions-complete",
    calendar: Path | None = None,
) -> ResearchInput:
    announced = []
    if actions:
        for raw in json.loads(actions.read_text(encoding="utf-8")):
            announced.append(
                AnnouncedAction(
                    CorporateAction(
                        raw["security_id"],
                        raw["action_type"],
                        date.fromisoformat(raw["ex_date"]),
                        date.fromisoformat(raw["pay_date"])
                        if raw.get("pay_date")
                        else None,
                        D(raw["split_ratio"])
                        if raw.get("split_ratio") is not None
                        else None,
                        D(raw["cash_amount"])
                        if raw.get("cash_amount") is not None
                        else None,
                    ),
                    timestamp(raw["available_at"]),
                )
            )
    clock = (
        tuple(date.fromisoformat(s) for s in json.loads(sessions.read_text()))
        if sessions
        else ()
    )
    return ResearchInput(
        read_csv(prices, require_complete=False),
        read_security_history(securities),
        tuple(announced),
        clock,
        basis,
        tuple(date.fromisoformat(s) for s in json.loads(calendar.read_text()))
        if calendar
        else (),
    )


def watch(
    agent: ResearchAgent,
    study: str,
    provider: Callable[[], ResearchInput],
    output: Path,
    *,
    max_cycles: int,
    interval: float = 30,
    max_ticks: int | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if (
        not 1 <= interval <= 60
        or max_ticks is not None
        and (type(max_ticks) is not int or max_ticks < 1)
    ):
        raise AgentError(
            "Watch interval must be 1..60 seconds and ticks must be positive"
        )
    tick, result = 0, {}
    while max_ticks is None or tick < max_ticks:
        # Errors stop the worker; an interrupted final evaluation is never
        # blindly retried. Complete evaluation evidence has an explicit recover.
        result = agent.tick(study, provider(), max_cycles=max_cycles)
        save_json(output / "agent.json", result)
        tick += 1
        shadow = result.get("shadow")
        if (
            result["validation_budget_exhausted"]
            and not result.get("committed_cycle")
            and shadow
            and shadow["observations"] >= result["status"]["policy"]["shadow_sessions"]
        ):
            break
        if max_ticks is None or tick < max_ticks:
            pause(interval)
    return result


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "action",
        choices=(
            "quality-screen",
            "quality-report",
            "bundle-publish",
            "backup",
            "restore",
            "reconcile-witness",
            "witness-status",
            "release-observation",
            "release-stop",
            "init",
            "run",
            "status",
            "recover",
            "abandon",
            "import-history",
            "demo",
        ),
    )
    p.add_argument("--db", type=Path, default=Path("artifacts/agent/research.sqlite3"))
    p.add_argument("--to", type=Path)
    p.add_argument("--from", dest="backup_source", type=Path)
    p.add_argument("--replace", action="store_true")
    p.add_argument("--operator")
    p.add_argument("--study", default="main")
    p.add_argument("--policy", type=Path)
    p.add_argument("--bundle", type=Path)
    p.add_argument("--source-meta", type=Path)
    p.add_argument("--csv", type=Path)
    p.add_argument("--securities", type=Path)
    p.add_argument("--actions", type=Path)
    p.add_argument(
        "--sessions",
        type=Path,
        help="JSON list from an explicit exchange-session provider; preserves missing bars",
    )
    p.add_argument(
        "--calendar",
        type=Path,
        help="JSON list of published exchange dates, including future sessions",
    )
    p.add_argument(
        "--corporate-action-basis", default="raw-unadjusted; supplied-actions-complete"
    )
    p.add_argument("--source-db", type=Path)
    p.add_argument("--approved-by")
    p.add_argument("--revision", type=int)
    p.add_argument("--release-id")
    p.add_argument("--stopped-by")
    p.add_argument("--cycle-id")
    p.add_argument("--reason", default="")
    p.add_argument("--output", type=Path, default=Path("artifacts/agent"))
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--max-ticks", type=int)
    p.add_argument(
        "--max-cycles",
        type=int,
        help="Total scheduled windows, capped by the immutable study budget",
    )
    return p


def restore(args: argparse.Namespace) -> dict[str, Any]:
    source = args.backup_source
    if source is None:
        raise AgentError("Restore requires --from")
    manifest = json.loads(source.with_suffix(".manifest.json").read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["sha256"]:
        raise AgentError("Backup hash mismatch")
    target = args.db
    if source.resolve() == target.resolve():
        raise AgentError("Restore target must differ from backup")
    files = [Path(str(target) + suffix) for suffix in ("", "-wal", "-shm")]
    if any(path.exists() for path in files) and not args.replace:
        raise AgentError("Target database exists; use --replace")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    preserved = [
        (path, Path(str(target) + ".replaced-" + stamp + str(path)[len(str(target)) :]))
        for path in files
        if path.exists()
    ]
    if any(dest.exists() for _, dest in preserved):
        raise AgentError("Preserved database name already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, suffix=".restore.tmp")
    os.close(descriptor)
    temp = Path(temporary)
    try:
        shutil.copyfile(source, temp)
        # Verify the staged bytes as well, before touching the existing database.
        if hashlib.sha256(temp.read_bytes()).hexdigest() != manifest["sha256"]:
            raise AgentError("Backup hash mismatch")
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        for path, dest in preserved:
            path.rename(dest)
        temp.rename(target)
    finally:
        temp.unlink(missing_ok=True)
    store = Store(target)
    try:
        result = ResearchAgent(store).witness_status()
        if result["missing"]:
            result["next_action"] = "reconcile-witness --operator NAME"
        return result
    finally:
        store.close()


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def quality_screen(args: argparse.Namespace) -> dict[str, Any]:
    """Split a raw price CSV into accepted and quarantined rows.

    The source file is only read. Quarantined rows keep their original values so
    a supplier defect can be audited and reported, never silently repaired.
    """
    if not args.csv:
        raise AgentError("quality-screen requires --csv")
    raw = args.csv.read_bytes()
    with args.csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or ())
        result = screen_price_rows(reader)
    args.output.mkdir(parents=True, exist_ok=True)
    accepted_path = args.output / "prices.accepted.csv"
    _write_csv(accepted_path, fields, result.accepted)
    _write_csv(
        args.output / "prices.quarantine.csv",
        ["line", "quality_codes", *fields],
        [
            {"line": q.line, "quality_codes": ";".join(q.codes), **q.row}
            for q in result.quarantined
        ],
    )
    report = {
        "source": str(args.csv),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "accepted_sha256": hashlib.sha256(accepted_path.read_bytes()).hexdigest(),
        "accepted": len(result.accepted),
        "quarantined": len(result.quarantined),
        "by_code": result.by_code(),
    }
    save_json(args.output / "quality-screen.json", report)
    return report


def quality_report(args: argparse.Namespace) -> dict[str, Any]:
    if args.bundle:
        data, manifest = open_bundle(args.bundle)
        source: dict[str, Any] = {"bundle_id": manifest["bundle_id"]}
    elif args.csv and args.securities:
        data = read_input(
            args.csv,
            args.securities,
            args.actions,
            args.sessions,
            args.corporate_action_basis,
            args.calendar,
        )
        source = {"csv": str(args.csv)}
    else:
        raise AgentError("quality-report requires --bundle or --csv and --securities")
    report = {**source, **assess(data).to_dict()}
    save_json(args.output / "quality-report.json", report)
    return {**source, "summary": report["summary"]}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if (
        args.action == "run"
        and args.bundle
        and any((args.csv, args.securities, args.actions, args.sessions, args.calendar))
    ):
        raise AgentError("--bundle cannot be combined with input file paths")
    if args.action == "quality-screen":
        return quality_screen(args)
    if args.action == "quality-report":
        return quality_report(args)
    if args.action == "bundle-publish":
        if not args.bundle or not args.csv or not args.securities:
            raise AgentError("bundle-publish requires --bundle, --csv and --securities")
        files = {"prices.csv": args.csv, "securities.jsonl": args.securities}
        for name in ("actions", "sessions", "calendar"):
            path = getattr(args, name)
            if path is not None:
                files[name + ".json"] = path
        sources = json.loads(args.source_meta.read_text()) if args.source_meta else {}
        return publish_bundle(
            args.bundle,
            files,
            basis=args.corporate_action_basis,
            sources=sources,
            clock=lambda: datetime.now(UTC),
        )
    if args.action == "restore":
        return restore(args)
    if args.action == "demo":
        from .demo import run_demo

        return run_demo(args.output)
    if args.action not in {"init", "import-history"} and not args.db.exists():
        raise AgentError("Initialize the persistent database first")
    if args.policy and args.action != "init":
        raise AgentError(
            "Policy is frozen; --policy is valid only during initialization"
        )
    if args.watch and args.action != "run":
        raise AgentError("--watch is valid only with run")
    store = Store(args.db)
    try:
        agent = ResearchAgent(store)
        if args.action == "backup":
            if args.to is None:
                raise AgentError("Backup requires --to")
            result = agent.backup(args.to)
        elif args.action == "witness-status":
            result = agent.witness_status()
        elif args.action == "reconcile-witness":
            result = agent.reconcile_witness(operator=args.operator)
        elif args.action == "init":
            policy = (
                policy_from_dict(json.loads(args.policy.read_text()))
                if args.policy
                else AgentPolicy()
            )
            result = agent.init(args.study, policy)
        elif args.action == "release-observation":
            result = agent.register_observation_release(
                approved_by=args.approved_by, revision=args.revision
            )
        elif args.action == "release-stop":
            result = agent.stop_observation_release(
                args.release_id,
                stopped_by=args.stopped_by,
                reason=args.reason or "HUMAN",
            )
        elif args.action == "status":
            result = agent.status(args.study)
        elif args.action == "import-history":
            if not args.source_db:
                raise AgentError("Historical consumption import requires --source-db")
            result = agent.import_history(args.source_db)
        elif args.action in {"recover", "abandon"}:
            if not args.cycle_id:
                raise AgentError("--cycle-id is required")
            result = (
                agent.recover(args.cycle_id)
                if args.action == "recover"
                else agent.abandon(args.cycle_id, args.reason)
            )
        else:
            if not args.bundle and (not args.csv or not args.securities):
                raise AgentError(
                    "Run requires --csv and --securities with explicit information timestamps"
                )
            budget = (
                args.max_cycles
                if args.max_cycles is not None
                else agent.status(args.study)["policy"]["max_validation_cycles"]
            )

            bundle_metadata: dict[str, Any] = {}

            def provider() -> ResearchInput:
                if args.bundle:
                    data, manifest = open_bundle(args.bundle)
                    bundle_metadata.update(
                        bundle_id=manifest["bundle_id"],
                        created_at=manifest["created_at"],
                    )
                    return data
                return read_input(
                    args.csv,
                    args.securities,
                    args.actions,
                    args.sessions,
                    args.corporate_action_basis,
                    args.calendar,
                )

            class BundleReporter(ResearchAgent):
                def tick(
                    self, name: str, data: ResearchInput, *, max_cycles: int = 1
                ) -> dict[str, Any]:
                    report = agent.tick(name, data, max_cycles=max_cycles)
                    if args.bundle:
                        report["input_bundle"] = dict(bundle_metadata)
                    return report

            result = watch(
                BundleReporter(store),
                args.study,
                provider,
                args.output,
                max_cycles=budget,
                interval=args.interval,
                max_ticks=args.max_ticks if args.watch else 1,
            )
        save_json(args.output / (args.action + ".json"), result)
        return result
    finally:
        store.close()


def main() -> int:
    try:
        args = parser().parse_args()
        result = run(args)
        if args.action in {
            "quality-screen",
            "quality-report",
            "bundle-publish",
            "backup",
            "restore",
            "witness-status",
            "reconcile-witness",
        }:
            print(canonical(result))
            return 0
        print(
            canonical(
                {
                    **(
                        {"input_bundle": result["input_bundle"]}
                        if "input_bundle" in result
                        else {}
                    ),
                    "study": result.get("study"),
                    "orders_submitted": 0,
                    "result": result.get(
                        "verification",
                        result.get(
                            "mode", result.get("waiting_for_data", result.get("status"))
                        ),
                    ),
                }
            )
        )
        return 0
    except KeyboardInterrupt:
        print(
            "Worker stopped. Stored evaluation claims are preserved.", file=sys.stderr
        )
        return 130
    except (AgentError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        print(f"Agent stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
