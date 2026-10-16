"""Run the four-stage research agent once or continuously on immutable inputs."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, canonical, timestamp
from trading_agent.data import read_csv
from trading_agent.selection import read_security_history
from trading_agent.store import Store

from momentum.ledger.ledger import CorporateAction

from .contracts import AgentPolicy, AnnouncedAction, ResearchInput, policy_from_dict
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
    p.add_argument("--study", default="main")
    p.add_argument("--policy", type=Path)
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


def run(args: argparse.Namespace) -> dict[str, Any]:
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
        if args.action == "init":
            policy = (
                policy_from_dict(json.loads(args.policy.read_text()))
                if args.policy
                else AgentPolicy()
            )
            result = agent.init(args.study, policy)
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
            if not args.csv or not args.securities:
                raise AgentError(
                    "Run requires --csv and --securities with explicit information timestamps"
                )
            budget = (
                args.max_cycles
                if args.max_cycles is not None
                else agent.status(args.study)["policy"]["max_validation_cycles"]
            )

            def provider() -> ResearchInput:
                return read_input(
                    args.csv,
                    args.securities,
                    args.actions,
                    args.sessions,
                    args.corporate_action_basis,
                    args.calendar,
                )

            result = watch(
                agent,
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
        result = run(parser().parse_args())
        print(
            canonical(
                {
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
