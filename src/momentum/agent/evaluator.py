"""One execution/accounting path for every screened research candidate.

KIS supplies point-in-time selection and planning; the original US Ledger owns
money, positions, corporate actions and independently reconciled NAV.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP
from decimal import Decimal as D
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from trading_agent.contracts import (
    AgentError,
    Costs,
    Decision,
    Wallet,
    digest,
)
from trading_agent.data import MarketView
from trading_agent.selection import RankedStrategy, SelectionEngine, select_candidates
from trading_agent.strategy import fit_buy_quantity, rebalance_plan

from momentum.execution.engine import Fill
from momentum.ledger.ledger import CorporateAction, Ledger

from .contracts import AgentPolicy, ResearchInput, is_decision_session
from .market import profile
from .snapshot import IndexedSnapshot

BENCHMARKS = ("cash", "equal_weight_eligible")
NAV_RESIDUAL_ABSOLUTE_TOLERANCE = D("1e-9")


class BenchmarkUnavailable(AgentError):
    """A configured reference lacks PIT coverage; no performance can be claimed."""


def enabled_benchmarks(policy: AgentPolicy) -> tuple[str, ...]:
    return BENCHMARKS + (
        ("market_etf",) if policy.market_etf_symbol is not None else ()
    )


def benchmark_version(kind: str, symbol: str | None = None) -> str:
    if kind == "market_etf":
        return digest(
            {
                "benchmark": kind,
                "definition": "fixed-etf-daily-risk-capped-v1",
                "symbol": symbol,
            }
        )
    return digest({"benchmark": kind, "definition": "eligible-all-equal-v1"})


def benchmark_decision(
    kind: str,
    selector: SelectionEngine,
    view: MarketView,
    valid_until: datetime,
    wallet: Wallet,
    diagnostics: dict[str, int] | None = None,
    *,
    etf_symbol: str | None = None,
) -> Decision:
    version = benchmark_version(kind, etf_symbol)
    empty = Decision(version, view.snapshot_id, view.as_of, valid_until, {})
    if kind == "cash":
        return empty
    policy = selector.policy
    marks = {
        s: h[-1].close
        for s, h in view.histories.items()
        if h and h[-1].session == view.expected_session
    }
    equity = wallet.equity(marks)
    if kind == "market_etf":
        state = selector.securities.states(view.as_of).get(etf_symbol)
        if state is None:
            raise BenchmarkUnavailable(
                "Market ETF metadata unavailable at observation time"
            )
        if state.asset_type != "ETF":
            raise BenchmarkUnavailable("Configured market benchmark must be an ETF")
        if (
            etf_symbol not in marks
            or len(view.histories[etf_symbol]) < policy.min_history
        ):
            raise BenchmarkUnavailable("Market ETF price/history coverage unavailable")
    # Reuse every existing market/quality/affordability filter. Positive prices
    # imply momentum > -1; neither ranking nor positive momentum selects slots.
    probe = RankedStrategy(
        lookback=2,
        top_n=1,
        gross_weight=min(policy.max_gross, policy.max_position),
        min_momentum=D(-1),
    )
    screened = select_candidates(
        probe,
        view,
        selector.securities,
        policy,
        selector.costs,
        valid_until,
        equity,
        audit=False,
    )
    if kind == "market_etf":
        row = next(r for r in screened["candidates"] if r["symbol"] == etf_symbol)
        if row["status"] not in {"SELECTED", "NOT_SELECTED"}:
            if diagnostics is not None:
                diagnostics["screening_rejection_sessions"] = 1
            return empty
        # A single fixed instrument still obeys position/order limits. This is
        # a daily risk-capped reference, not a fully invested index proxy.
        assert etf_symbol is not None
        return replace(
            empty, target_weights={etf_symbol: screened["position_budget"] / equity}
        )
    eligible_symbols = {
        row["symbol"]
        for row in screened["candidates"]
        if row["status"] in {"SELECTED", "NOT_SELECTED"}
    }
    count = len(eligible_symbols)
    budget_reason = ["UNAFFORDABLE_WITHIN_POSITION_BUDGET"]
    initially_unaffordable = sum(
        row["reasons"] == budget_reason for row in screened["candidates"]
    )
    if diagnostics is not None:
        # The denominator excludes quality/market failures, but includes stocks
        # that cannot buy one share even at the maximum position budget.
        diagnostics["budget_candidates"] = count + initially_unaffordable
        diagnostics["unaffordable_candidates"] = initially_unaffordable
    if not count:
        return empty
    allocation = replace(
        probe,
        top_n=count,
        gross_weight=min(policy.max_gross, count * policy.max_position),
    )
    # Recheck affordability at the fixed equal-weight budget. Rejected slots
    # remain cash rather than increasing the other stocks' allocations.
    allocated = select_candidates(
        allocation,
        view,
        selector.securities,
        policy,
        selector.costs,
        valid_until,
        equity,
        audit=False,
    )
    if diagnostics is not None:
        diagnostics["unaffordable_candidates"] += sum(
            row["symbol"] in eligible_symbols and row["reasons"] == budget_reason
            for row in allocated["candidates"]
        )
    return replace(allocated["decision"], strategy_version=version)


@dataclass(frozen=True)
class MarketCosts(Costs):
    commission_bps: D = D("1.5")
    sell_tax_bps: D = D(0)
    slippage_bps: D = D(5)
    quantum: D = D(1)

    def fee(self, side: str, quantity: int, price: D) -> D:
        rate = self.commission_bps + (self.sell_tax_bps if side == "SELL" else D(0))
        return (quantity * price * rate / D(10000)).quantize(
            self.quantum, ROUND_HALF_UP
        )

    def reserve_fee(self, side: str, quantity: int, price: D) -> D:
        rate = self.commission_bps + (self.sell_tax_bps if side == "SELL" else D(0))
        return (price * rate / D(10000)).quantize(
            self.quantum, ROUND_CEILING
        ) * quantity

    def execution_price(self, side: str, price: D) -> D:
        direction = D(1) if side == "BUY" else D(-1)
        return (price * (1 + direction * self.slippage_bps / D(10000))).quantize(
            self.quantum, ROUND_HALF_UP
        )


class LedgerWallet(Wallet):
    """NAV includes unpaid entitlements; buying power is still actual cash."""

    def __init__(self, ledger: Ledger):
        super().__init__(ledger.cash, {s: q for s, q in ledger.positions.items() if q})
        self.receivables = ledger.dividend_receivable
        self.carried = carried_marks(ledger)

    def equity(self, marks: dict[str, D]) -> D:
        # A holding without a bar today (halt, quarantined row) is valued at
        # the ledger's last close, never at an invented price; it cannot trade
        # either way because it has no open.
        return super().equity({**self.carried, **marks}) + self.receivables


def carried_marks(ledger: Ledger) -> dict[str, D]:
    """Last known close of every held security (zero once written off)."""
    return {s: ledger.marks[s] for s, q in ledger.positions.items() if q}


@dataclass(frozen=True)
class EvaluationContract:
    schema_version: str
    evaluation_window: tuple[str, str]
    data_bundle_id: str
    market: str
    currency: str
    money_quantum: str
    execution_clock: tuple[str, str]
    valuation_basis: str
    capital: tuple[D, D, str]
    risk_limits: tuple[D, D, D]
    initial_portfolio: str
    execution_model: tuple[D, str, str, str]
    cost_scenario: tuple[str, D, D, D]
    ledger_version: str
    metrics_spec_version: str
    selection_policy_id: str
    nav_residual_absolute_tolerance: D
    benchmark_definitions: tuple[tuple[str, str], ...]
    # Strategies only; benchmarks keep their daily definitions.
    decision_cadence: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        # Keep the frozen contract deeply immutable while giving reports named fields.
        for name, keys in (
            ("capital", ("initial", "limit", "rule")),
            ("risk_limits", ("max_gross", "max_position", "max_order_notional")),
            ("execution_clock", ("observation", "execution")),
            (
                "execution_model",
                ("volume_cap_ratio", "order_priority", "fill", "slippage"),
            ),
            (
                "cost_scenario",
                ("name", "commission_bps", "sell_tax_bps", "slippage_bps"),
            ),
        ):
            result[name] = dict(zip(keys, result[name], strict=True))
        return result

    @property
    def contract_id(self) -> str:
        return digest(self.to_dict())


@lru_cache(maxsize=1)
def ledger_version() -> str:
    return hashlib.sha256(
        (Path(__file__).parents[1] / "ledger" / "ledger.py").read_bytes()
    ).hexdigest()


@dataclass(frozen=True)
class EvaluationResult:
    strategy_version: str
    start: str
    end: str
    net_return: float
    max_drawdown: float
    trades: int
    turnover: float
    fees: str
    daily_returns: tuple[float, ...]
    equity_curve: tuple[dict[str, Any], ...]
    trade_log: tuple[dict[str, Any], ...]
    execution_events: tuple[dict[str, Any], ...]
    evaluation_id: str
    tradability: dict[str, Any]
    turnover_on_initial_capital: float
    turnover_on_average_nav: float
    evaluation_contract_id: str
    evaluation_contract: dict[str, Any]
    portfolio_diagnostics: dict[str, Any]


def portfolio_diagnostics(
    curve: list[dict[str, Any]], *, benchmark: str | None
) -> dict[str, Any]:
    """Session-close exposure and candidate-session affordability, never gate inputs."""
    observations = len(curve)
    candidates = sum(row["budget_candidates"] for row in curve)
    unaffordable = sum(row["unaffordable_candidates"] for row in curve)
    holdings = [sum(q > 0 for q in row["positions"].values()) for row in curve]
    result = {
        "sampling": "equal-weighted evaluation session closes",
        "average_holding_count": sum(holdings) / observations,
        "last_holding_count": holdings[-1],
        "max_holding_count": max(holdings),
        "budget_screening_applies": benchmark == "equal_weight_eligible",
        "budget_candidate_observations": candidates,
        "unaffordable_observations": unaffordable,
        "unaffordable_rate": unaffordable / candidates if candidates else None,
        "unaffordable_rate_basis": "candidate-session counts after other filters, before affordability",
        "max_abs_nav_residual": str(max(abs(D(row["nav_residual"])) for row in curve)),
        "screening_rejection_sessions": sum(
            row["screening_rejection_sessions"] for row in curve
        ),
    }
    for name, field in (
        ("invested", "positions_value"),
        ("cash", "cash"),
        ("receivable", "receivable"),
    ):
        result[f"average_{name}_weight"] = float(
            sum((D(row[field]) / D(row["equity"]) for row in curve), D(0))
            / observations
        )
    return result


def require_same_contract(a: EvaluationResult, b: EvaluationResult) -> None:
    for result in (a, b):
        if digest(result.evaluation_contract) != result.evaluation_contract_id:
            raise AgentError("Evaluation contract payload does not match its ID")
    if a.evaluation_contract_id != b.evaluation_contract_id:
        differing = sorted(
            k
            for k in a.evaluation_contract.keys() | b.evaluation_contract.keys()
            if a.evaluation_contract.get(k) != b.evaluation_contract.get(k)
        )
        raise AgentError(
            "Results are not under the same evaluation contract: "
            + ", ".join(differing)
        )


def tradability_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Use a common pre-execution mark, including orders that never filled."""
    keys = (
        "fill_ratio",
        "volume_cap_reduction",
        "cash_risk_reduction",
        "cancel_reduction",
    )
    result: dict[str, Any] = dict.fromkeys(keys)
    result["order_count_fill_ratio"] = (
        sum(e["quantity"] == e["desired_quantity"] for e in events) / len(events)
        if events
        else None
    )
    result["missing_reference_price"] = [
        {key: e[key] for key in ("session", "symbol", "side")}
        for e in events
        if e.get("reference_price") is None
    ]
    result["cancel_reasons"] = {}
    if result["missing_reference_price"]:
        return result
    denominator = D(0)
    numerators = [D(0)] * 4
    reasons: dict[str, D] = {}
    for e in events:
        desired, capped, fitted, filled = (
            D(str(e[k]))
            for k in (
                "desired_quantity",
                "volume_capped_quantity",
                "cash_risk_capped_quantity",
                "quantity",
            )
        )
        if not 0 <= filled <= fitted <= capped <= desired:
            raise AgentError("Inconsistent tradability quantity stages")
        ref = D(e["reference_price"])
        if not ref.is_finite() or ref <= 0:
            raise AgentError("Invalid tradability reference price")
        denominator += desired * ref
        for i, quantity in enumerate(
            (filled, desired - capped, capped - fitted, fitted - filled)
        ):
            numerators[i] += quantity * ref
        if fitted > filled:
            reason = e["status"]
            reasons[reason] = reasons.get(reason, D(0)) + (fitted - filled) * ref
    if denominator:
        ratios = [float(n / denominator) for n in numerators]
        if not math.isclose(sum(ratios), 1.0, rel_tol=1e-12, abs_tol=1e-12):
            raise AgentError("Tradability ratios do not reconcile")
        result.update(zip(keys, ratios, strict=True))
        result["cancel_reasons"] = {
            k: float(v / denominator) for k, v in sorted(reasons.items())
        }
    return result


class EvaluatorPort(Protocol):
    def evaluate(
        self,
        data: ResearchInput,
        spec: RankedStrategy,
        start: date,
        end: date,
        *,
        stress: bool = False,
    ) -> EvaluationResult: ...


class CommonEvaluator:
    def __init__(self, policy: AgentPolicy):
        self.policy = policy

    def evaluate(
        self,
        data: ResearchInput,
        spec: RankedStrategy,
        start: date,
        end: date,
        *,
        stress: bool = False,
    ) -> EvaluationResult:
        return self._evaluate(data, spec, start, end, stress=stress)

    def evaluate_schedule(
        self,
        data: ResearchInput,
        schedule: list[tuple[date, RankedStrategy]],
        start: date,
        end: date,
        *,
        stress: bool = False,
    ) -> EvaluationResult:
        if (
            not schedule
            or schedule[0][0] > start
            or any(a[0] > b[0] for a, b in pairwise(schedule))
        ):
            raise AgentError("Schedule requires ordered entries beginning by start")
        return self._evaluate(
            data, schedule[0][1], start, end, stress=stress, schedule=schedule
        )

    def evaluate_benchmark(
        self,
        data: ResearchInput,
        kind: str,
        start: date,
        end: date,
        *,
        stress: bool = False,
    ) -> EvaluationResult:
        if kind == "market_etf" and self.policy.market_etf_symbol is None:
            raise AgentError("Market ETF must be explicitly configured")
        if kind not in enabled_benchmarks(self.policy):
            raise AgentError("Unknown registered benchmark")
        return self._evaluate(
            data, self.policy.logic.champion, start, end, stress=stress, benchmark=kind
        )

    def _evaluate(
        self,
        data: ResearchInput,
        spec: RankedStrategy,
        start: date,
        end: date,
        *,
        stress: bool,
        benchmark: str | None = None,
        schedule: list[tuple[date, RankedStrategy]] | None = None,
    ) -> EvaluationResult:
        p = self.policy
        quantum = D(p.money_quantum)
        c = p.logic.costs
        costs = MarketCosts(c.commission_bps, c.sell_tax_bps, c.slippage_bps, quantum)
        if stress:
            costs = replace(
                costs,
                commission_bps=costs.commission_bps * 2,
                slippage_bps=costs.slippage_bps * 2,
            )
        selected = [i for i, d in enumerate(data.sessions) if start <= d <= end]
        if len(selected) < 2 or not selected or selected[0] == 0:
            raise AgentError(
                "Evaluation requires prior information and two executable sessions"
            )
        # Fractional KRW price inputs need an explicit tick/price model, never
        # silent rounding of the underlying history into a convenient fixture.
        snapshot = data.snapshot
        if (
            not snapshot.whole_prices(quantum)
            if isinstance(snapshot, IndexedSnapshot)
            else any(
                v != v.quantize(quantum)
                for b in snapshot.bars
                for v in (b.open, b.close, b.high, b.low)
            )
        ):
            raise AgentError(
                "KRW raw prices must match the declared whole-won precision"
            )
        capital = p.logic.selection.capital_limit
        selection = p.logic.selection
        contract = EvaluationContract(
            schema_version="krx-evaluation-v2",
            evaluation_window=(start.isoformat(), end.isoformat()),
            data_bundle_id=data.fingerprint(end),
            market=p.market,
            currency=p.currency,
            money_quantum=p.money_quantum,
            execution_clock=(
                f"prior_session {profile(p.market).observation_time:%H:%M} Asia/Seoul",
                f"next_session {profile(p.market).execution_open:%H:%M} Asia/Seoul",
            ),
            valuation_basis="mark_to_close_carry_last_delisted_zero",
            capital=(capital, selection.capital_limit, "min(nav, capital_limit)"),
            risk_limits=(
                selection.max_gross,
                selection.max_position,
                selection.max_order_notional,
            ),
            initial_portfolio="cash_only_reset_per_evaluation",
            execution_model=(
                profile(p.market).max_volume_participation,
                "sell_first",
                "next_open",
                "open_raw_with_fixed_slippage",
            ),
            cost_scenario=(
                "stress_2x_commission_slippage" if stress else "base",
                costs.commission_bps,
                costs.sell_tax_bps,
                costs.slippage_bps,
            ),
            ledger_version=ledger_version(),
            metrics_spec_version="reference-notional-tradability-v1;turnover-dual-v1;portfolio-diagnostics-v1;nav-residual-v1",
            selection_policy_id=digest(selection),
            nav_residual_absolute_tolerance=NAV_RESIDUAL_ABSOLUTE_TOLERANCE,
            decision_cadence=p.decision_cadence,
            benchmark_definitions=tuple(
                (kind, benchmark_version(kind, p.market_etf_symbol))
                for kind in enabled_benchmarks(p)
            ),
        )
        ledger = Ledger(capital, money_quantum=quantum, fractional_split="drop")
        market = profile(p.market)
        selection_policy = p.logic.selection
        if benchmark == "market_etf" and "ETF" not in selection_policy.allowed_types:
            # A stock-only strategy universe must not hide its own benchmark from
            # screening or execution; every other filter still applies (RES-01b).
            selection_policy = replace(
                selection_policy,
                allowed_types=(*selection_policy.allowed_types, "ETF"),
            )
        selector = SelectionEngine(data.securities, selection_policy, costs)
        curve: list[dict[str, Any]] = []
        trade_log: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        daily: list[float] = []
        prior, peak, drawdown = capital, capital, D(0)
        fees, turnover = D(0), D(0)
        boundaries: list[dict[str, Any]] = []
        active_version: str | None = None
        for i in selected:
            day, prior_day = data.sessions[i], data.sessions[i - 1]
            if schedule is not None:
                spec = next(
                    s for effective, s in reversed(schedule) if effective <= day
                )
            changed = active_version is not None and active_version != spec.version
            active_version = spec.version
            fees_before = fees
            at = market.observed_at(prior_day)
            view = data.snapshot.view(at, prior_day, allow_missing=True)
            required = max(
                p.logic.selection.min_history, 3 if benchmark else spec.lookback + 1
            )
            expected = set(data.sessions[max(0, i - required) : i])
            histories = {}
            for symbol, history in view.histories.items():
                # Expected sessions are the latest ones up to prior_day, so only
                # the last len(expected) bars can cover them; scanning the whole
                # history every session made long evaluations quadratic.
                missing = expected - {b.session for b in history[-len(expected) :]}
                # Keep the actual contiguous tail, including a current mark
                # for a holding. An absent exchange session cannot disappear
                # from the observation window just because every feed missed it.
                histories[symbol] = (
                    history[
                        bisect.bisect_right(
                            history, max(missing), key=lambda b: b.session
                        ) :
                    ]
                    if missing
                    else history
                )
            view = replace(view, histories=histories)
            marks = {
                **carried_marks(ledger),
                **{
                    s: h[-1].close
                    for s, h in view.histories.items()
                    if h and h[-1].session == prior_day
                },
            }
            wallet = LedgerWallet(ledger)
            screening: dict[str, int] = {}
            # Between decision sessions a strategy holds what it has: no
            # selection, no orders. Corporate actions, marks and write-offs
            # below still run every session.
            decide = (
                benchmark is not None
                or changed
                or is_decision_session(
                    p.decision_cadence, day, None if i == selected[0] else prior_day
                )
            )
            plan: list[Any] = []
            desired_plan: list[Any] = []
            if decide:
                decision = (
                    benchmark_decision(
                        benchmark,
                        selector,
                        view,
                        market.decision_deadline(day),
                        wallet,
                        screening,
                        etf_symbol=p.market_etf_symbol,
                    )
                    if benchmark
                    else selector(spec, view, market.decision_deadline(day), wallet)
                )
                caps = {
                    s: int(h[-1].volume * market.max_volume_participation)
                    for s, h in view.histories.items()
                    if h and h[-1].session == prior_day
                }
                # No opening price is supplied to the original planning step.
                plan = rebalance_plan(wallet, decision, marks, costs, caps)
                desired_plan = rebalance_plan(wallet, decision, marks, costs, None)
            capped_plan = {(s, side): q for s, side, q, _ in plan}
            actions = [
                a
                for a in data.actions
                if a.action.ex_date == day or a.action.pay_date == day
            ]
            # A late dividend is paid through the ordinary pay-date branch;
            # its ex-date branch never fires because ex_date is in the past.
            late_paid = [
                CorporateAction(
                    d.security_id,
                    "cash_dividend",
                    d.ex_date,
                    d.pay_date,
                    None,
                    d.cash_amount,
                )
                for d in data.late_dividends
                if d.pay_date == day
            ]
            ledger.apply_corporate_actions([a.action for a in actions] + late_paid, day)
            for late in data.late_dividends:
                if late.recognized_on == day:
                    ledger.accrue_entitlement(
                        late.security_id,
                        late.ex_date,
                        late.cash_amount,
                        late.pay_date,
                        day,
                    )
            rows = data.snapshot.day(day)
            opens = {s: b.open for s, b in rows.items()}
            splits = {
                a.action.security_id: a
                for a in actions
                if a.action.action_type == "split" and a.action.ex_date == day
            }
            # Bonus and reduction days change what a share is worth against the
            # prior close while the sellable quantity lags (policy B), so an
            # order planned before them is not rescaled but cancelled.
            reshaped = {
                a.action.security_id
                for a in actions
                if a.action.action_type in {"bonus_issue", "capital_reduction"}
                and a.action.ex_date == day
            }
            for symbol, side, desired, _ in desired_plan:
                planned = capped_plan.get((symbol, side), 0)
                uncertain_action = symbol in reshaped
                action = splits.get(symbol)
                if action is not None:
                    ratio = action.action.split_ratio
                    assert ratio is not None
                    adjusted = D(planned) * ratio
                    adjusted_desired = D(desired) * ratio
                    if (
                        action.available_at > at
                        or adjusted != adjusted.to_integral_value()
                    ):
                        uncertain_action = True
                    else:
                        planned = int(adjusted)
                        # An uncapped demand may be fractional after a split;
                        # reporting it must not cancel an otherwise integer fill.
                        desired = (
                            int(adjusted_desired)
                            if adjusted_desired == adjusted_desired.to_integral_value()
                            else str(adjusted_desired)
                        )
                event = {
                    "session": day.isoformat(),
                    "signal_session": prior_day.isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "planned_quantity": planned,
                    "desired_quantity": desired,
                    "volume_capped_quantity": planned,
                    "cash_risk_capped_quantity": planned,
                    "reference_price": str(marks[symbol]) if symbol in marks else None,
                    "quantity": 0,
                    "status": "cancelled_no_open",
                }
                if uncertain_action or not planned:
                    event["status"] = (
                        "cancelled_action_uncertainty"
                        if uncertain_action
                        else "cancelled_volume_cap"
                    )
                    events.append(event)
                    continue
                if symbol not in opens:
                    events.append(event)
                    continue
                if not selector.execution_allowed(symbol, side, market.open_at(day)):
                    event["status"] = "cancelled_security_state"
                    events.append(event)
                    continue
                quantity = planned
                event["open_price"] = str(opens[symbol])
                event["fill_price_source"] = "open_raw_with_fixed_slippage"
                price = costs.execution_price(side, opens[symbol])
                if price <= 0:
                    raise AgentError(
                        "Nonpositive execution price after declared rounding"
                    )
                current = LedgerWallet(ledger)
                if side == "BUY":
                    selection = p.logic.selection
                    quantity = fit_buy_quantity(
                        current,
                        symbol,
                        quantity,
                        price,
                        {**carried_marks(ledger), **opens},
                        costs,
                        selection.max_gross,
                        selection.max_position,
                        selection.max_order_notional,
                        capital,
                    )
                else:
                    quantity = min(quantity, ledger.positions.get(symbol, 0))
                event["cash_risk_capped_quantity"] = quantity
                event["quantity"] = quantity
                event["status"] = (
                    "filled" if quantity == planned else "reduced_cash_or_risk"
                )
                events.append(event)
                if not quantity:
                    continue
                fee = costs.fee(side, quantity, price)
                ledger.apply_fills(
                    [
                        Fill(
                            symbol,
                            side.lower(),
                            quantity,
                            price,
                            fee,
                            str(event["status"]),
                            day,
                            prior_day,
                            "open_raw_with_fixed_slippage",
                        )
                    ],
                    day,
                )
                fees += fee
                turnover += quantity * price
                trade_log.append({**event, "price": str(price), "fee": str(fee)})
            closes = {s: b.close for s, b in rows.items()}
            ledger.mark_to_market(closes, day)
            # A held security that is delisted by this observation has no price
            # left to sell at: write it off rather than carry its last close.
            delisted = selector.securities.states(market.observed_at(day))
            written_off = []
            for symbol in sorted(s for s, q in ledger.positions.items() if q):
                state = delisted.get(symbol)
                if state is not None and state.status == "DELISTED":
                    result = ledger.write_off(symbol, day)
                    if result is not None:
                        written_off.append(
                            {
                                "symbol": symbol,
                                "shares": result[0],
                                "last_mark": str(result[1]),
                            }
                        )
            stale_value = ledger.stale_positions_value(day)
            independent_nav = (
                ledger.cash + ledger.positions_value + ledger.dividend_receivable
            )
            residual = ledger.nav - independent_nav
            if (
                not residual.is_finite()
                or abs(residual) > NAV_RESIDUAL_ABSOLUTE_TOLERANCE
            ):
                raise AgentError(
                    f"Independent ledger NAV residual {residual} exceeds absolute "
                    f"tolerance {NAV_RESIDUAL_ABSOLUTE_TOLERANCE} KRW"
                )
            # Blended acquisition marks can be recurring Decimals. Reconcile
            # the independent ledger NAV at the declared currency precision;
            # cash, close-valued holdings and entitlements remain exact won.
            equity = ledger.nav.quantize(quantum, ROUND_HALF_UP)
            if equity != independent_nav or ledger.cash < 0:
                raise AgentError(
                    f"Independent ledger reconciliation failed ({day}: "
                    f"equity {equity}, independent {independent_nav}, "
                    f"cash {ledger.cash})"
                )
            daily.append(float(equity / prior - 1))
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
            curve.append(
                {
                    "session": day.isoformat(),
                    "equity": str(equity),
                    "cash": str(ledger.cash),
                    "positions_value": str(ledger.positions_value),
                    "receivable": str(ledger.dividend_receivable),
                    "positions": dict(ledger.positions),
                    "stale_value": str(stale_value),
                    "written_off": written_off,
                    "fractions_dropped": [
                        {"symbol": s, "shares_before": q, "fraction": str(f)}
                        for s, d, q, f in ledger.fractions_dropped
                        if d == day
                    ],
                    "nav_residual": str(residual),
                    "budget_candidates": screening.get("budget_candidates", 0),
                    "unaffordable_candidates": screening.get(
                        "unaffordable_candidates", 0
                    ),
                    "screening_rejection_sessions": screening.get(
                        "screening_rejection_sessions", 0
                    ),
                }
            )
            if changed:
                boundaries.append(
                    {
                        "session": day.isoformat(),
                        "spec_version": spec.version,
                        "nav_before": str(prior),
                        "nav_after": str(equity),
                        "fees": str(fees - fees_before),
                    }
                )
            prior = equity
        if any(not math.isfinite(r) for r in daily):
            raise AgentError("Nonfinite evaluation result")
        diagnostics = portfolio_diagnostics(curve, benchmark=benchmark)
        if schedule is not None:
            diagnostics["schedule"] = {"revision_boundaries": boundaries}
        return EvaluationResult(
            benchmark_version(benchmark, p.market_etf_symbol)
            if benchmark
            else spec.version,
            start.isoformat(),
            end.isoformat(),
            float(prior / capital - 1),
            float(drawdown),
            len(trade_log),
            float(turnover / capital),
            str(fees),
            tuple(daily),
            tuple(curve),
            tuple(trade_log),
            tuple(events),
            digest(
                {
                    "data": data.fingerprint(end),
                    "policy": p,
                    "spec": {"benchmark": benchmark} if benchmark else spec,
                    **({"schedule": schedule} if schedule is not None else {}),
                    "start": start,
                    "end": end,
                    "stress": stress,
                }
            ),
            tradability_metrics(events),
            float(turnover / capital),
            float(turnover / (sum((D(r["equity"]) for r in curve), D(0)) / len(curve))),
            contract.contract_id,
            contract.to_dict(),
            diagnostics,
        )
