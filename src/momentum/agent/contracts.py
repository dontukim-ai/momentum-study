"""Versioned contracts; policy is immutable throughout a research study."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, aware, digest, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings, settings_from_dict
from trading_agent.selection import SecurityHistory

from momentum.ledger.ledger import CorporateAction


@lru_cache(maxsize=1)
def build_id() -> str:
    import trading_agent

    roots = [Path(__file__).parents[1], Path(trading_agent.__file__).parent]
    return digest(
        {
            f"{i}/{p.relative_to(root)}": hashlib.sha256(p.read_bytes()).hexdigest()
            for i, root in enumerate(roots)
            for p in sorted(root.rglob("*.py"))
        }
    )


@dataclass(frozen=True)
class AnnouncedAction:
    action: CorporateAction
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", aware(self.available_at))
        a = self.action
        if (
            a.action_type not in {"split", "cash_dividend"}
            or a.action_type == "split"
            and (
                a.split_ratio is None
                or not a.split_ratio.is_finite()
                or a.split_ratio <= 0
            )
            or a.action_type == "cash_dividend"
            and (
                a.pay_date is None
                or a.pay_date <= a.ex_date
                or a.cash_amount is None
                or not a.cash_amount.is_finite()
                or a.cash_amount < 0
            )
        ):
            raise AgentError("Unsupported or incomplete corporate action")
        if self.available_at > session_time(a.ex_date, 9):
            raise AgentError(
                "Late corporate-action coverage cannot support a chronological evaluation"
            )


@dataclass(frozen=True)
class ResearchInput:
    snapshot: Snapshot
    securities: SecurityHistory
    actions: tuple[AnnouncedAction, ...] = ()
    sessions: tuple[date, ...] = ()
    corporate_action_basis: str = "raw-unadjusted; supplied-actions-complete"

    def __post_init__(self) -> None:
        if not self.sessions:
            object.__setattr__(self, "sessions", self.snapshot.sessions)
        if tuple(sorted(set(self.sessions))) != self.sessions or not set(
            self.snapshot.sessions
        ) <= set(self.sessions):
            raise AgentError("Explicit sorted unique session clock required")
        if not self.corporate_action_basis.strip():
            raise AgentError("Corporate-action coverage declaration required")
        keys = [
            (a.action.security_id, a.action.action_type, a.action.ex_date)
            for a in self.actions
        ]
        if len(keys) != len(set(keys)):
            raise AgentError("Duplicate corporate actions")
        for a in self.actions:
            if (
                a.action.ex_date <= self.sessions[-1]
                and a.action.ex_date not in self.sessions
                or a.action.pay_date is not None
                and a.action.pay_date <= self.sessions[-1]
                and a.action.pay_date not in self.sessions
            ):
                raise AgentError("Corporate action must use declared exchange sessions")

    def prefix(self, end: date) -> ResearchInput:
        clock = session_time(end, 16)
        return ResearchInput(
            Snapshot(
                [b for b in self.snapshot.bars if b.session <= end],
                require_complete=False,
            ),
            SecurityHistory(list(self.securities.visible(clock))),
            tuple(a for a in self.actions if a.available_at <= clock),
            tuple(d for d in self.sessions if d <= end),
            self.corporate_action_basis,
        )

    def fingerprint(self, end: date) -> str:
        prefix = self.prefix(end)
        return digest(
            {
                "prices": prefix.snapshot.id,
                "securities": prefix.securities.id,
                "actions": tuple(
                    a for a in self.actions if a.available_at <= session_time(end, 16)
                ),
                "sessions": prefix.sessions,
                "basis": prefix.corporate_action_basis,
            }
        )


@dataclass(frozen=True)
class AgentPolicy:
    logic: LogicSettings = field(default_factory=LogicSettings)
    market: str = "KRX"
    currency: str = "KRW"
    money_quantum: str = "1"
    warmup_sessions: int = 61
    development_sessions: int = 120
    holdout_sessions: int = 40
    embargo_sessions: int = 5
    shadow_sessions: int = 20
    max_validation_cycles: int = 8
    bootstrap_block: int = 5
    bootstrap_samples: int = 2000
    family_alpha: float = 0.05

    def __post_init__(self) -> None:
        if (self.market, self.currency, self.money_quantum) != ("KRX", "KRW", "1"):
            raise AgentError(
                "This adapter supports KRX/KRW only; US H1-H4 final evaluation remains locked"
            )
        if (
            self.logic.selection.capital_limit
            != self.logic.selection.capital_limit.quantize(Decimal(self.money_quantum))
        ):
            raise AgentError("Capital must match the declared currency precision")
        integers = (
            "warmup_sessions",
            "development_sessions",
            "holdout_sessions",
            "embargo_sessions",
            "shadow_sessions",
            "max_validation_cycles",
            "bootstrap_block",
            "bootstrap_samples",
        )
        if any(type(getattr(self, n)) is not int for n in integers):
            raise AgentError("Research session counts and budgets must be integers")
        if (
            self.warmup_sessions < max(61, self.logic.selection.min_history)
            or self.development_sessions < 90
            or self.holdout_sessions < 30
            or self.embargo_sessions < self.bootstrap_block
            or self.shadow_sessions < 2
            or not 1 <= self.max_validation_cycles <= 20
            or not 1 <= self.bootstrap_block <= self.holdout_sessions // 4
            or not 0.001 <= self.family_alpha <= 0.05
            or self.bootstrap_samples < 2000
        ):
            raise AgentError("Invalid preregistered schedule or statistical budget")
        if (
            self.bootstrap_samples
            * self.family_alpha
            / (2 * self.max_validation_cycles)
            < 2
        ):
            raise AgentError(
                "Too few bootstrap draws for the registered validation budget"
            )


def policy_from_dict(raw: dict[str, Any]) -> AgentPolicy:
    try:
        values = dict(raw)
        if "logic" in values:
            values["logic"] = settings_from_dict(values["logic"])
        return AgentPolicy(**values)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentError("Invalid immutable agent policy") from exc


def require_observed(data: ResearchInput, now: datetime | None = None) -> None:
    if session_time(data.sessions[-1], 16) > aware(now or datetime.now(UTC)):
        raise AgentError("Input includes sessions that have not closed yet")
