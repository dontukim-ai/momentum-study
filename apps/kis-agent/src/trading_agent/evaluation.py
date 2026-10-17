"""Daily open execution proxy for screening, never a live-fill guarantee."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .contracts import AgentError, Costs, D, StrategySpec, Wallet, digest, session_time
from .data import Snapshot
from .strategy import decide, fit_buy_quantity, rebalance_plan


@dataclass(frozen=True)
class BacktestResult:
    strategy_version: str
    snapshot_id: str
    start: str
    end: str
    initial_cash: str
    final_equity: str
    net_return: float
    max_drawdown: float
    annualized_volatility: float
    sharpe: float | None
    turnover: float
    fees: str
    trades: int
    daily_returns: tuple[float, ...]
    equity_curve: tuple[dict, ...]
    trade_log: tuple[dict, ...]
    assumptions: tuple[str, ...]
    evaluation_id: str


def backtest(snapshot: Snapshot, spec: StrategySpec, costs: Costs = Costs(),
             initial_cash: Decimal = D("10000000"), start: date | None = None,
             end: date | None = None, participation: Decimal = D("0.01"), *,
             decision_engine=None, evaluation_binding: dict | None = None,
             risk_policy=None, capital_limit: Decimal | None = None, execution_filter=None) -> BacktestResult:
    if initial_cash <= 0 or not D(0) < participation <= D(1):
        raise AgentError("Invalid capital or participation")
    custom = any(value is not None for value in (decision_engine, evaluation_binding, risk_policy, capital_limit, execution_filter))
    if custom and any(value is None for value in (decision_engine, evaluation_binding, risk_policy, capital_limit)):
        raise AgentError("Custom logic requires immutable evaluation binding, risk and capital limits")
    eligible = [i for i, day in enumerate(snapshot.sessions)
                if i > 0 and (start is None or day >= start) and (end is None or day <= end)]
    if len(eligible) < 2:
        raise AgentError("Evaluation requires at least two executable sessions")
    wallet = Wallet(initial_cash)
    curve, trades, daily = [], [], []
    total_fees, notional = D(0), D(0)
    prior, peak, drawdown = initial_cash, initial_cash, D(0)
    for i in eligible:
        day, prior_day = snapshot.sessions[i], snapshot.sessions[i - 1]
        now = session_time(day, 9)
        # Signal uses the prior close only after its publication; fills use next session.
        view = snapshot.view(session_time(prior_day, 16), prior_day, allow_missing=decision_engine is not None)
        decision = (decide(spec, view, now + timedelta(hours=1)) if decision_engine is None else
                    decision_engine(spec, view, now + timedelta(hours=1), Wallet(wallet.cash, dict(wallet.positions))))
        rows = snapshot.day(day)
        opens = {s: b.open for s, b in rows.items()}
        prior_closes = {s: h[-1].close for s, h in view.histories.items() if h and h[-1].session == prior_day}
        caps = {s: int(h[-1].volume * participation) if h and h[-1].session == prior_day else 0 for s, h in view.histories.items()}
        # Freeze desired shares using signal-time marks. Opening prices may only
        # determine fills or reduce an already planned buy for cash/risk limits.
        for symbol, side, quantity, _signal_price in rebalance_plan(wallet, decision, prior_closes, costs, caps):
            if symbol not in opens:
                continue  # Never fill a missing opening price at a prior close.
            price = costs.execution_price(side, opens[symbol])
            if execution_filter is not None and not execution_filter(symbol, side, now):
                continue
            if side == "BUY":
                limits = {} if risk_policy is None else {"max_gross": risk_policy.max_gross,
                         "max_position": risk_policy.max_position, "max_notional": risk_policy.max_order_notional,
                         "capital_limit": capital_limit}
                quantity = fit_buy_quantity(wallet, symbol, quantity, price, opens, costs, **limits)
            if not quantity:
                continue
            fee = costs.fee(side, quantity, price)
            wallet.fill(symbol, side, quantity, price, fee)
            total_fees += fee
            notional += quantity * price
            trades.append({"session": day.isoformat(), "signal_session": prior_day.isoformat(),
                           "symbol": symbol, "side": side, "quantity": quantity,
                           "price": str(price), "fee": str(fee), "decision_time": view.as_of.isoformat()})
        equity = wallet.equity({s: b.close for s, b in rows.items()})
        daily.append(float(equity / prior - D(1)))
        peak = max(peak, equity)
        drawdown = max(drawdown, D(1) - equity / peak)
        curve.append({"session": day.isoformat(), "equity": str(equity), "cash": str(wallet.cash),
                      "positions": dict(wallet.positions)})
        prior = equity
    std = statistics.stdev(daily) if len(daily) > 1 else 0.0
    mean = statistics.mean(daily)
    binding = {"engine": "daily-open-screen-v1", "snapshot": snapshot.id,
                            "strategy": spec.version, "costs": costs, "cash": initial_cash,
                            "start": start, "end": end, "participation": participation}
    if evaluation_binding is not None:
        binding["logic_context"] = evaluation_binding
        binding["risk_policy"], binding["capital_limit"] = risk_policy, capital_limit
    evaluation_id = digest(binding)
    return BacktestResult(spec.version, snapshot.id, curve[0]["session"], curve[-1]["session"],
                          str(initial_cash), str(prior), float(prior / initial_cash - D(1)),
                          float(drawdown), std * math.sqrt(252), mean / std * math.sqrt(252) if std else None,
                          float(notional / initial_cash), str(total_fees), len(trades), tuple(daily),
                          tuple(curve), tuple(trades), (
                              "SCREENING ONLY: prior close signal, next-session open plus fixed slippage",
                              "Full fills up to a cap based on prior available daily volume; no intraday queue model",
                              "No corporate actions, distributions, FX, tick rounding or delisted-universe reconstruction",
                              "Example fees; replace with verified instrument/date/account-specific costs",
                              ("Bound policy and research capital ceiling applied; no live order authorization" if risk_policy else
                               "Local demo caps: 80% gross, 40% per symbol, 5000000 notional per order; not an approved live policy"),
                          ), evaluation_id)
