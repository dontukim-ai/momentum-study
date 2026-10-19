"""Offline software validation; engineered returns are never market evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

from trading_agent.contracts import Bar
from trading_agent.data import Snapshot, write_csv
from trading_agent.logic import LogicSettings
from trading_agent.selection import (
    RankedStrategy,
    SecurityHistory,
    SecurityState,
    write_security_history,
)
from trading_agent.store import Store

from .contracts import AgentPolicy, ResearchInput
from .market import KRX
from .service import ResearchAgent


def fixture(count: int = 460, *, flat: bool = False) -> ResearchInput:
    bars: list[Any] = []
    states: list[Any] = []
    day, close = date(2024, 1, 2), D(10000)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = close
            if not flat:
                close = (
                    opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))
                ).quantize(D(1))
            bars.append(
                Bar(
                    day,
                    "ENGINEERED",
                    opening,
                    max(opening, close),
                    min(opening, close),
                    close,
                    100000,
                    KRX.final_bar_at(day),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
            states.append(
                SecurityState(
                    "ENGINEERED",
                    KRX.close_at(day),
                    KRX.final_bar_at(day),
                    "NORMAL",
                    "STOCK",
                    "KOSPI",
                    False,
                    False,
                    False,
                    False,
                    close * D(".9995"),
                    close * D("1.0005"),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
        day += timedelta(days=1)
    return ResearchInput(Snapshot(bars), SecurityHistory(states))


def run_demo(output: Path) -> dict[str, Any]:
    from .cli import save_json

    output.mkdir(parents=True, exist_ok=True)
    data = fixture()
    policy = AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(20), lookbacks=(2, 20), rankings=("momentum",)
        ),
        holdout_sessions=80,
    )
    write_csv(data.snapshot, output / "prices.csv")
    write_security_history(data.securities, output / "securities.jsonl")
    save_json(output / "policy.json", policy)
    save_json(output / "sessions.json", data.sessions)
    store = Store(output / "research.sqlite3")
    try:
        agent = ResearchAgent(store)
        agent.init("engineered", policy)
        first = agent.tick("engineered", data, max_cycles=2)
        repeated = agent.tick("engineered", data, max_cycles=2)
        # A separate flat fixture proves rejection without selecting a more
        # favorable strategy or relaxing the statistical policy.
        flat = fixture(230, flat=True)
        flat_store = Store(output / "flat-research.sqlite3")
        try:
            flat_agent = ResearchAgent(flat_store)
            flat_agent.init("flat", replace(policy, holdout_sessions=40))
            kept = flat_agent.tick("flat", flat)
        finally:
            flat_store.close()
        assert first["cycles"][0]["research_champion_updated"]
        assert first["cycles"][1]["verdict"] == "KEEP_CHAMPION"
        assert all(c["cached"] for c in repeated["cycles"])
        assert not kept["cycles"][0]["research_champion_updated"]
        assert first["shadow"]["observations"] == policy.shadow_sessions
        assert not any(
            store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("orders", "accounts", "fills")
        )
        result = {
            "study": "engineered",
            "mode": "SYNTHETIC_SOFTWARE_VALIDATION",
            "verification": {
                "four_stages_completed": True,
                "research_promotion_verified": True,
                "subsequent_keep_verified": True,
                "flat_market_keep_verified": True,
                "same_input_cached": True,
                "forward_shadow_verified": True,
                "market_evidence": False,
                "external_calls": 0,
                "orders_submitted": 0,
            },
            "orders_submitted": 0,
        }
        save_json(output / "agent.json", first)
        save_json(output / "repeat.json", repeated)
        save_json(output / "flat.json", kept)
        save_json(output / "verification.json", result)
        return result
    finally:
        store.close()
