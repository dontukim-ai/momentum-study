from __future__ import annotations

from decimal import Decimal
from typing import Literal, NamedTuple

from momentum.config import ExecutionCfg


class TargetOrder(NamedTuple):
    security_id: str
    side: Literal["buy", "sell"]
    shares: int
    rank: int


def compute_targets(
    ranked: list[str],
    nav: Decimal,
    prev_close: dict[str, Decimal],
    holdings: dict[str, int],
    cfg: ExecutionCfg,
) -> list[TargetOrder]:
    """전일 종가만으로 목표 수량을 산정한다. 다음 시가는 파라미터로도 받지 않는다.

    A security that is selected AND already held but missing from `prev_close`
    produces no order at all (neither buy nor sell) and is left unmanaged for the
    caller. This is deliberate: CLAUDE.md rule 3 says a missing price cancels the
    BUY decision — it does not authorize treating the gap as grounds to force a
    SELL. Liquidating a position because its price feed went missing would be
    inventing a decision the data doesn't support, and would also violate the
    "never silently drop a position" principle in the other direction (an unwanted
    forced exit instead of an unwanted forced hold). Task 9 receives no order for
    that security and must not assume that means "already at target" — it means
    the price to size a trade around was unavailable at prev-close.
    """
    selected = ranked[: cfg.max_positions]
    selected_set = set(selected)
    sells: list[TargetOrder] = []
    buys: list[TargetOrder] = []

    # 1) 선정에서 빠진 보유 종목은 전량 매도. 매도가 항상 먼저다.
    for sid in sorted(holdings):
        qty = holdings[sid]
        if sid not in selected_set and qty > 0:
            sells.append(TargetOrder(sid, "sell", qty, -1))

    if not selected:
        return sells

    # 가중치는 선정 종목 수(N)를 기준으로 하며, 종가 결측으로 스킵되는
    # 종목이 있어도 나머지 종목의 비중을 재분배하지 않는다.
    weight = min(Decimal(1) / Decimal(len(selected)), Decimal(str(cfg.max_weight)))
    investable = nav * (Decimal(1) - Decimal(str(cfg.cash_buffer)))

    for rank, sid in enumerate(selected):
        px = prev_close.get(sid)
        if px is None or px <= 0:
            continue
        target = int((investable * weight) // px)
        delta = target - holdings.get(sid, 0)
        if delta > 0:
            buys.append(TargetOrder(sid, "buy", delta, rank))
        elif delta < 0:
            sells.append(TargetOrder(sid, "sell", -delta, -1))

    return sells + buys
