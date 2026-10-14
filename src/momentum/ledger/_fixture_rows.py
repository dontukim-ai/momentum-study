"""String-fixture-row parsing, kept off the production critical path.

``Ledger.step_session`` is the single typed entry point into the fixed
processing order. These parsers exist only to translate the golden-fixture
CSV's compact string encodings (``buy:AAA:600:0``, ``div:AAA:0.50:2026-01-09:
2026-01-12``, ...) into the typed values ``step_session`` wants, so that
``Ledger.step(row)`` can stay a thin convenience wrapper for the fixture
format without boxing in Task 10, which will call ``step_session`` directly
with real ``Decimal``/``date``/``TargetOrder`` values and never touch these
parsers.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from momentum.config import SpecUndefinedError
from momentum.ledger.ledger import CorporateAction
from momentum.portfolio.targets import TargetOrder


def _items(field: str) -> list[str]:
    return [part for part in (field or "").split(";") if part.strip()]


def _pairs(field: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (field or "").split(";"):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        out[key.strip()] = value.strip()
    return out


def parse_opens(field: str) -> dict[str, Decimal | None]:
    return {k: (Decimal(v) if v else None) for k, v in _pairs(field).items()}


def parse_closes(field: str) -> dict[str, Decimal]:
    return {k: Decimal(v) for k, v in _pairs(field).items() if v}


def parse_adv(field: str) -> dict[str, float]:
    return {k: float(v) for k, v in _pairs(field).items() if v}


def parse_orders(field: str) -> list[TargetOrder]:
    orders: list[TargetOrder] = []
    for item in _items(field):
        side, sid, shares, rank = item.split(":")
        if side == "buy":
            orders.append(TargetOrder(sid, "buy", int(shares), int(rank)))
        elif side == "sell":
            orders.append(TargetOrder(sid, "sell", int(shares), int(rank)))
        else:
            raise SpecUndefinedError(f"unknown order side {side!r}")
    return orders


def parse_actions(field: str) -> list[CorporateAction]:
    cas: list[CorporateAction] = []
    for item in _items(field):
        parts = item.split(":")
        kind = parts[0]
        if kind == "div":
            _, sid, amount, ex, pay = parts
            cas.append(
                CorporateAction(
                    sid,
                    "cash_dividend",
                    date.fromisoformat(ex),
                    date.fromisoformat(pay),
                    None,
                    Decimal(amount),
                )
            )
        elif kind == "split":
            _, sid, ratio, ex = parts
            cas.append(
                CorporateAction(
                    sid, "split", date.fromisoformat(ex), None, Decimal(ratio), None
                )
            )
        else:
            raise SpecUndefinedError(f"unknown corporate action token {kind!r}")
    return cas


def parse_row(
    row: dict[str, str],
) -> tuple[
    date,
    date,
    list[CorporateAction],
    list[TargetOrder],
    dict[str, Decimal | None],
    dict[str, float],
    dict[str, Decimal],
]:
    d = date.fromisoformat(row["session_date"])
    asof = date.fromisoformat(row["asof"])
    actions = parse_actions(row.get("actions", ""))
    orders = parse_orders(row.get("orders", ""))
    opens = parse_opens(row.get("opens", ""))
    adv = parse_adv(row.get("adv", ""))
    closes = parse_closes(row.get("closes", ""))
    return d, asof, actions, orders, opens, adv, closes
