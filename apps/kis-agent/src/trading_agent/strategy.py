"""Pure strategy output and shared integer-share portfolio planning."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from .contracts import Costs, D, Decision, StrategySpec, Wallet
from .data import MarketView


def decide(spec: StrategySpec, view: MarketView, valid_until: datetime) -> Decision:
    scores = []
    for symbol, history in view.histories.items():
        if len(history) > spec.lookback:
            momentum = history[-1].close / history[-spec.lookback - 1].close - D(1)
            if momentum > spec.min_momentum:
                scores.append((momentum, symbol))
    chosen = sorted(scores, key=lambda x: (-x[0], x[1]))[:spec.top_n]
    # Missing positive signals leave their slot in cash; no concentration increase.
    weights = {symbol: spec.gross_weight / spec.top_n for _, symbol in chosen}
    return Decision(spec.version, view.snapshot_id, view.as_of, valid_until, weights)


def rebalance_plan(wallet: Wallet, decision: Decision, prices: dict[str, Decimal], costs: Costs,
                   caps: dict[str, int] | None = None) -> list[tuple[str, str, int, Decimal]]:
    equity = wallet.equity(prices)
    targets = {s: int(equity * w / prices[s]) for s, w in decision.target_weights.items()}
    sells, buys = [], []
    for symbol in sorted(set(wallet.positions) | set(targets)):
        delta = targets.get(symbol, 0) - wallet.positions.get(symbol, 0)
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        quantity = abs(delta)
        if caps is not None:
            quantity = min(quantity, caps.get(symbol, 0))
        if quantity:
            item = (symbol, side, quantity, costs.execution_price(side, prices[symbol]))
            (buys if side == "BUY" else sells).append(item)
    return sells + buys


def fit_buy_quantity(wallet: Wallet, symbol: str, quantity: int, price: Decimal,
                     marks: dict[str, Decimal], costs: Costs, max_gross: Decimal = D(".8"),
                     max_position: Decimal = D(".4"), max_notional: Decimal = D("5000000"),
                     capital_limit: Decimal | None = None) -> int:
    """Shared conservative sizing for screening and local execution, after each fill."""
    equity = wallet.equity(marks)
    if capital_limit is not None:
        equity = min(equity, capital_limit)
    held = wallet.positions.get(symbol, 0) * marks[symbol]
    gross = sum((q * marks[s] for s, q in wallet.positions.items()), D(0))
    # Reserve fees for the worst per-share partial-fill rounding case.
    cash_quantity = int(wallet.cash / (price + costs.reserve_fee("BUY", 1, price)))
    capacity = min(max_notional, equity * max_position - held, equity * max_gross - gross)
    return max(0, min(quantity, cash_quantity, int(capacity / max(price, marks[symbol]))))
