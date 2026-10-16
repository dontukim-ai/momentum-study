"""One execution/accounting path for every screened research candidate.

KIS supplies point-in-time selection and planning; the original US Ledger owns
money, positions, corporate actions and independently reconciled NAV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP
from decimal import Decimal as D
from typing import Any, Protocol

from trading_agent.contracts import AgentError, Costs, Wallet, digest, session_time
from trading_agent.selection import RankedStrategy, SelectionEngine
from trading_agent.strategy import fit_buy_quantity, rebalance_plan

from momentum.execution.engine import Fill
from momentum.ledger.ledger import Ledger

from .contracts import AgentPolicy, ResearchInput


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

    def equity(self, marks: dict[str, D]) -> D:
        return super().equity(marks) + self.receivables


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
        if any(
            v != v.quantize(quantum)
            for b in data.snapshot.bars
            for v in (b.open, b.close, b.high, b.low)
        ):
            raise AgentError(
                "KRW raw prices must match the declared whole-won precision"
            )
        capital = p.logic.selection.capital_limit
        ledger = Ledger(capital, money_quantum=quantum)
        selector = SelectionEngine(data.securities, p.logic.selection, costs)
        curve: list[dict[str, Any]] = []
        trade_log: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        daily: list[float] = []
        prior, peak, drawdown = capital, capital, D(0)
        fees, turnover = D(0), D(0)
        for i in selected:
            day, prior_day = data.sessions[i], data.sessions[i - 1]
            at = session_time(prior_day, 16)
            view = data.snapshot.view(at, prior_day, allow_missing=True)
            required = max(p.logic.selection.min_history, spec.lookback + 1)
            expected = set(data.sessions[max(0, i - required) : i])
            histories = {}
            for symbol, history in view.histories.items():
                missing = expected - {b.session for b in history}
                # Keep the actual contiguous tail, including a current mark
                # for a holding. An absent exchange session cannot disappear
                # from the observation window just because every feed missed it.
                histories[symbol] = (
                    tuple(b for b in history if b.session > max(missing))
                    if missing
                    else history
                )
            view = replace(view, histories=histories)
            marks = {
                s: h[-1].close
                for s, h in view.histories.items()
                if h and h[-1].session == prior_day
            }
            wallet = LedgerWallet(ledger)
            decision = selector(spec, view, session_time(day, 10), wallet)
            caps = {
                s: int(h[-1].volume * D(".01"))
                for s, h in view.histories.items()
                if h and h[-1].session == prior_day
            }
            # No opening price is supplied to the original planning step.
            plan = rebalance_plan(wallet, decision, marks, costs, caps)
            actions = [
                a
                for a in data.actions
                if a.action.ex_date == day or a.action.pay_date == day
            ]
            ledger.apply_corporate_actions([a.action for a in actions], day)
            rows = data.snapshot.day(day)
            opens = {s: b.open for s, b in rows.items()}
            splits = {
                a.action.security_id: a
                for a in actions
                if a.action.action_type == "split" and a.action.ex_date == day
            }
            for symbol, side, planned, _ in plan:
                action = splits.get(symbol)
                if action is not None:
                    ratio = action.action.split_ratio
                    assert ratio is not None
                    adjusted = D(planned) * ratio
                    if (
                        action.available_at > at
                        or adjusted != adjusted.to_integral_value()
                    ):
                        events.append(
                            {
                                "session": day.isoformat(),
                                "symbol": symbol,
                                "status": "cancelled_action_uncertainty",
                            }
                        )
                        continue
                    planned = int(adjusted)
                event = {
                    "session": day.isoformat(),
                    "signal_session": prior_day.isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "planned_quantity": planned,
                    "quantity": 0,
                    "status": "cancelled_no_open",
                }
                if symbol not in opens:
                    events.append(event)
                    continue
                if not selector.execution_allowed(symbol, side, session_time(day, 9)):
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
                        opens,
                        costs,
                        selection.max_gross,
                        selection.max_position,
                        selection.max_order_notional,
                        capital,
                    )
                else:
                    quantity = min(quantity, ledger.positions.get(symbol, 0))
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
            if any(q and s not in closes for s, q in ledger.positions.items()):
                raise AgentError(
                    "Missing held close; evaluation fails rather than inventing a mark"
                )
            ledger.mark_to_market(closes, day)
            if (
                ledger.nav
                != ledger.cash + ledger.positions_value + ledger.dividend_receivable
                or ledger.cash < 0
            ):
                raise AgentError("Independent ledger reconciliation failed")
            equity = ledger.nav
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
                }
            )
            prior = equity
        if any(not math.isfinite(r) for r in daily):
            raise AgentError("Nonfinite evaluation result")
        return EvaluationResult(
            spec.version,
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
                    "spec": spec,
                    "start": start,
                    "end": end,
                    "stress": stress,
                }
            ),
        )
