"""Market-on-open execution.

Fixed processing order for a session (the ledger owns the corporate-action and
mark-to-market halves): sells first, then the cash they freed is available to
the buys that follow, buys in the order given (Task 8 emits them rank-ascending).

A missing open never falls back to the previous close — CLAUDE.md rule 1 and
rule 3 both forbid it — so such an order is reported as ``cancelled_no_open``
with zero shares and a zero price rather than filled at a price that does not
exist. Every fill therefore carries ``price_source="open_raw"`` plus the session
it executed on and the ``asof`` observation date the order was sized from, so a
downstream test can prove the next open was used and not the signal-day close.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from momentum.config import ExecutionCfg, SpecUndefinedError
from momentum.execution.costs import apply_caps, one_way_cost
from momentum.portfolio.targets import TargetOrder

PRICE_SOURCE = "open_raw"


class Fill(NamedTuple):
    security_id: str
    side: str
    shares: int
    price: Decimal
    cost: Decimal
    status: str
    session_date: date
    asof: date
    price_source: str
    reduction_reasons: tuple[str, ...] = ()


def _affordable_shares(desired: int, price: Decimal, cash: Decimal, bps: float) -> int:
    """Largest n <= desired with n*price + one_way_cost(n*price, bps) <= cash."""
    if price <= 0 or cash <= 0:
        return 0
    n = min(desired, int(cash // price))
    while n > 0 and n * price + one_way_cost(n * price, bps) > cash:
        n -= 1
    return n


def execute(
    orders: list[TargetOrder],
    opens: dict[str, Decimal | None],
    adv: dict[str, float],
    cash: Decimal,
    cfg: ExecutionCfg,
    *,
    session_date: date,
    asof: date,
) -> list[Fill]:
    bps = cfg.base_cost_bps
    fills: list[Fill] = []
    available = cash

    def _cancelled(order: TargetOrder) -> Fill:
        return Fill(
            order.security_id,
            order.side,
            0,
            Decimal(0),
            Decimal(0),
            "cancelled_no_open",
            session_date,
            asof,
            PRICE_SOURCE,
        )

    def _adv_for(order: TargetOrder) -> float:
        # CLAUDE.md rule 3: a security with a live order but no ADV entry is
        # missing data, not zero liquidity. Filling it as zero shares would
        # silently vanish a buy or trap a sell forever (finding 5); refuse
        # instead.
        if order.security_id not in adv:
            raise SpecUndefinedError(
                f"no ADV data for {order.security_id}; cannot size participation "
                f"cap for a live order"
            )
        return adv[order.security_id]

    for order in (o for o in orders if o.side == "sell"):
        price = opens.get(order.security_id)
        if price is None or price <= 0:
            # No liquidation price (missing open, delisting): hold the position.
            fills.append(_cancelled(order))
            continue
        shares, reason = apply_caps(order.shares, _adv_for(order), cfg)
        sell_reasons: tuple[str, ...] = (reason,) if reason is not None else ()
        status = "filled" if reason is None else "reduced_participation"
        notional = shares * price
        cost = one_way_cost(notional, bps)
        available += notional - cost
        fills.append(
            Fill(
                order.security_id,
                "sell",
                shares,
                price,
                cost,
                status,
                session_date,
                asof,
                PRICE_SOURCE,
                sell_reasons,
            )
        )

    for order in (o for o in orders if o.side == "buy"):
        price = opens.get(order.security_id)
        if price is None or price <= 0:
            fills.append(_cancelled(order))
            continue
        capped, reason = apply_caps(order.shares, _adv_for(order), cfg)
        reasons: list[str] = [reason] if reason is not None else []
        status = "filled" if reason is None else "reduced_participation"
        shares = _affordable_shares(capped, price, available, bps)
        if shares < capped:
            status = "reduced_cash"
            reasons.append("insufficient_cash")
        notional = shares * price
        cost = one_way_cost(notional, bps)
        available -= notional + cost
        fills.append(
            Fill(
                order.security_id,
                "buy",
                shares,
                price,
                cost,
                status,
                session_date,
                asof,
                PRICE_SOURCE,
                tuple(reasons),
            )
        )

    return fills
