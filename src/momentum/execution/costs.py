from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from math import floor

from momentum.config import ExecutionCfg

CENT = Decimal("0.01")


def one_way_cost(notional: Decimal, bps: float, quantum: Decimal = CENT) -> Decimal:
    """Cost rounded half-up at the money quantum: cents by default, whole won for a KRW ledger."""
    return (notional * Decimal(str(bps)) / Decimal(10000)).quantize(quantum, ROUND_HALF_UP)


def participation_cap(adv_shares: float, cfg: ExecutionCfg) -> int:
    return max(0, floor(adv_shares * cfg.participation_limit))


def apply_caps(
    desired_shares: int, adv_shares: float, cfg: ExecutionCfg
) -> tuple[int, str | None]:
    cap = participation_cap(adv_shares, cfg)
    if desired_shares <= cap:
        return desired_shares, None
    return cap, "participation_limit"
