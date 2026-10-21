"""Versioned contracts; policy is immutable throughout a research study."""

from __future__ import annotations

import bisect
import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, aware, digest
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings, settings_from_dict
from trading_agent.selection import SecurityHistory, SecurityState

from momentum.ledger.ledger import CorporateAction

from .market import KRX, PROFILES

# Preregistered before results: conservatively covers at least half a 1-KRW
# tick near the 1,000-KRW price floor and the gap above the default 5 bps.
HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS = Decimal("25")  # noqa: FURB157


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
            a.action_type
            not in {"split", "cash_dividend", "bonus_issue", "capital_reduction"}
            or a.action_type == "bonus_issue"
            and (
                a.split_ratio is None
                or not a.split_ratio.is_finite()
                or a.split_ratio <= 0
                or a.pay_date is None
                or a.pay_date <= a.ex_date
            )
            or a.action_type == "capital_reduction"
            and (
                a.split_ratio is None
                or not a.split_ratio.is_finite()
                or not 0 < a.split_ratio < 1
            )
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
        if self.available_at > KRX.open_at(a.ex_date):
            raise AgentError(
                "Late corporate-action coverage cannot support a chronological evaluation"
            )


@dataclass(frozen=True)
class LateDividend:
    """A cash dividend whose amount was published after its ex-date.

    Under the pre-2023 Korean practice the record date (and so the ex-date)
    comes first and the board fixes the amount weeks later. The holding at
    the ex-date is entitled, but no evaluation may see the amount before
    ``available_at``; the ledger books it on ``recognized_on``, the first
    session whose open follows publication (policy A, 2026-10-20).
    """

    security_id: str
    ex_date: date
    recognized_on: date
    pay_date: date
    cash_amount: Decimal
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", aware(self.available_at))
        if not self.cash_amount.is_finite() or self.cash_amount < 0:
            raise AgentError("Late dividend needs a finite non-negative amount")
        if not self.ex_date < self.recognized_on < self.pay_date:
            raise AgentError("Late dividend needs ex_date < recognized_on < pay_date")
        if self.available_at > KRX.open_at(self.recognized_on):
            raise AgentError("Late dividend recognized before it was published")
        if self.available_at <= KRX.open_at(self.ex_date):
            raise AgentError("Dividend known before its ex-date is an ordinary action")


class SessionConfirmedSecurities(SecurityHistory):
    """Event-sourced security states, reconfirmed at every observed session.

    Historical status files record only changes, derived from a complete
    daily quote scan: a state without a newer event still held on every later
    session in ``sessions``. Selection judges freshness by ``effective_at``, so
    a carried-forward state is shown as effective at the open of the latest
    session whose observation has passed, exactly what a daily snapshot file
    would contain. Nothing is reconfirmed past the last declared session.
    """

    def __init__(self, records: list[SecurityState], sessions: tuple[date, ...]):
        super().__init__(records)
        if not sessions or tuple(sorted(set(sessions))) != tuple(sessions):
            raise AgentError("Session-confirmed securities need a sorted session clock")
        self.sessions = tuple(sessions)
        self._observed = [KRX.observed_at(d) for d in self.sessions]
        self._confirmed_cache: dict[datetime, dict[str, SecurityState]] = {}
        records_id: str = digest(self.records)
        self.id = digest(
            {
                "records": records_id,
                "confirmation": "event-sourced-session-confirmed-v1",
                "through": self.sessions[-1],
            }
        )

    def states(self, at: datetime) -> dict[str, SecurityState]:
        at = aware(at)
        if at in self._confirmed_cache:
            return dict(self._confirmed_cache[at])
        result = super().states(at)
        index = bisect.bisect_right(self._observed, at)
        if index:
            confirmed = KRX.open_at(self.sessions[index - 1])
            result = {
                symbol: replace(state, effective_at=confirmed)
                if state.effective_at < confirmed
                else state
                for symbol, state in result.items()
            }
        self._confirmed_cache[at] = result
        return dict(result)

    def until(self, end: date, clock: datetime) -> SessionConfirmedSecurities:
        return SessionConfirmedSecurities(
            list(self.visible(clock)), tuple(d for d in self.sessions if d <= end)
        )


class _ObservedCalendar(tuple[date, ...]):
    """Keep an omitted calendar implicit across dataclasses.replace(sessions=...)."""


@dataclass(frozen=True)
class ResearchInput:
    snapshot: Snapshot
    securities: SecurityHistory
    actions: tuple[AnnouncedAction, ...] = ()
    sessions: tuple[date, ...] = ()
    corporate_action_basis: str = "raw-unadjusted; supplied-actions-complete"
    calendar: tuple[date, ...] = ()
    late_dividends: tuple[LateDividend, ...] = ()

    def __post_init__(self) -> None:
        if not self.sessions:
            object.__setattr__(self, "sessions", self.snapshot.sessions)
        if tuple(sorted(set(self.sessions))) != self.sessions or not set(
            self.snapshot.sessions
        ) <= set(self.sessions):
            raise AgentError("Explicit sorted unique session clock required")
        if not self.calendar or (
            isinstance(self.calendar, _ObservedCalendar)
            and self.calendar[-1] <= self.sessions[-1]
        ):
            object.__setattr__(self, "calendar", _ObservedCalendar(self.sessions))
        elif isinstance(self.calendar, _ObservedCalendar):
            # A copied calendar extending past observations is now explicit.
            object.__setattr__(self, "calendar", tuple(self.calendar))
        if (
            tuple(sorted(set(self.calendar))) != self.calendar
            or tuple(d for d in self.calendar if d <= self.sessions[-1])
            != self.sessions
        ):
            raise AgentError("Exchange calendar must match observed sessions exactly")
        if not self.corporate_action_basis.strip():
            raise AgentError("Corporate-action coverage declaration required")
        keys = [
            (a.action.security_id, a.action.action_type, a.action.ex_date)
            for a in self.actions
        ]
        keys += [
            (d.security_id, "cash_dividend", d.ex_date) for d in self.late_dividends
        ]
        if len(keys) != len(set(keys)):
            raise AgentError("Duplicate corporate actions")
        for late in self.late_dividends:
            if any(
                day <= self.sessions[-1] and day not in self.sessions
                for day in (late.ex_date, late.recognized_on, late.pay_date)
            ):
                raise AgentError("Corporate action must use declared exchange sessions")
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
        clock = KRX.observed_at(end)
        return ResearchInput(
            Snapshot(
                [b for b in self.snapshot.bars if b.session <= end],
                require_complete=False,
            ),
            self.securities.until(end, clock)
            if isinstance(self.securities, SessionConfirmedSecurities)
            else SecurityHistory(list(self.securities.visible(clock))),
            tuple(a for a in self.actions if a.available_at <= clock),
            tuple(d for d in self.sessions if d <= end),
            self.corporate_action_basis,
            ()
            if isinstance(self.calendar, _ObservedCalendar)
            else tuple(d for d in self.calendar if d <= end),
            tuple(d for d in self.late_dividends if d.available_at <= clock),
        )

    def fingerprint(self, end: date) -> str:
        prefix = self.prefix(end)
        content: dict[str, Any] = {
            "prices": prefix.snapshot.id,
            "securities": prefix.securities.id,
            "actions": tuple(
                a for a in self.actions if a.available_at <= KRX.observed_at(end)
            ),
            "sessions": prefix.sessions,
            "basis": prefix.corporate_action_basis,
        }
        # Added only when present so inputs without late dividends keep the
        # fingerprints already stored by existing studies.
        if prefix.late_dividends:
            content["late_dividends"] = prefix.late_dividends
        return digest(content)


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
    statistical_policy_version: str = "v1"
    min_tail_draws: int = 50
    market_etf_symbol: str | None = None
    memory_policy: str = "none"
    memory_cooldown_cycles: int = 2

    observation_checkpoint_sessions: int = 20
    degradation_reference: str = "equal_weight_eligible"
    degradation_underperformance: float = 0.03
    degradation_drawdown: float | None = None
    demotion_checkpoints: int = 3
    execution_min_observation_sessions: int = 60

    def __post_init__(self) -> None:
        if self.memory_policy not in ("none", "suppress_failed_final_v1"):
            raise AgentError("Invalid preregistered memory policy")
        if (
            type(self.memory_cooldown_cycles) is not int
            or self.memory_cooldown_cycles < 1
        ):
            raise AgentError("Invalid preregistered memory cooldown")
        if (
            type(self.execution_min_observation_sessions) is not int
            or self.execution_min_observation_sessions < 1
        ):
            raise AgentError("Invalid preregistered execution observation minimum")
        if (
            self.logic.selection.spread_mode == "historical_unavailable"
            and self.logic.costs.slippage_bps < HISTORICAL_SPREAD_MIN_SLIPPAGE_BPS
        ):
            raise AgentError(
                "Historical spread-unavailable mode requires preregistered slippage of at least 25 bps"
            )
        ratios = [self.degradation_underperformance]
        if self.degradation_drawdown is not None:
            ratios.append(self.degradation_drawdown)
        if (
            type(self.observation_checkpoint_sessions) is not int
            or self.observation_checkpoint_sessions < 2
            or type(self.demotion_checkpoints) is not int
            or self.demotion_checkpoints < 1
            or self.degradation_reference not in ("cash", "equal_weight_eligible")
            or any(
                type(value) not in (int, float) or not 0 < value < 1 for value in ratios
            )
        ):
            raise AgentError("Invalid preregistered degradation policy")
        if self.market_etf_symbol is not None and (
            not isinstance(self.market_etf_symbol, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", self.market_etf_symbol)
        ):
            raise AgentError("Invalid preregistered market ETF symbol")
        supported = PROFILES.get(self.market)
        if supported is None or (self.currency, self.money_quantum) != (
            supported.currency,
            supported.money_quantum,
        ):
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
            "min_tail_draws",
        )
        if any(type(getattr(self, n)) is not int for n in integers):
            raise AgentError("Research session counts and budgets must be integers")
        if (
            self.statistical_policy_version not in {"v1", "v2"}
            or self.min_tail_draws < 1
        ):
            raise AgentError("Invalid statistical policy version or tail budget")
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
        if self.statistical_policy_version == "v2" and (
            self.bootstrap_samples * self.family_alpha / self.max_validation_cycles
            < self.min_tail_draws
        ):
            raise AgentError("Too few bootstrap tail draws for statistical policy v2")


def policy_from_dict(raw: dict[str, Any]) -> AgentPolicy:
    try:
        values = dict(raw)
        if "logic" in values:
            values["logic"] = settings_from_dict(values["logic"])
        return AgentPolicy(**values)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AgentError("Invalid immutable agent policy") from exc


def require_observed(data: ResearchInput, now: datetime | None = None) -> None:
    if KRX.observed_at(data.sessions[-1]) > aware(now or datetime.now(UTC)):
        raise AgentError("Input includes sessions that have not closed yet")
