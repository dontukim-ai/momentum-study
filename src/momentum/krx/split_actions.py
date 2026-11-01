"""Split actions from detected adjustments that a prior disclosure announced (FIN-02b).

A detected split day (FIN-02a) is observed on the ex-date itself, so it may
only become a corporate action when an OpenDART split or reverse-split
disclosure for the same listed code was public before that day's open. The
action is then available from the *disclosure* time, never from the
detection. The ratio comes from the agreement of listed shares and the
exchange base price (FIN-02a); it is not read from the filing text, which
has no structured API, and the output records that basis.

Anything that does not reconcile cleanly (no prior disclosure, preferred
shares whose filing is under the common code, a ratio that is not a finite
decimal) is reported and produces no action, so its price jump stays
quarantined as an unexplained return.

User decision 2026-11-01 (docs/blueprint/ma-cross-v20.md): a detection whose
shares and base price do not agree on one ratio (``OTHER``) is no longer
dropped silently. With a prior split or reverse-split filing in the
direction of the move, the exchange's base-price ratio (a simple fraction
within one tick) is taken when the listed-share ratio lies within 30% of it
(odd lots, conversions and issues on the same day move the count); every
other ``OTHER`` detection is reported as unresolved. A capital-reduction
filing (B-type 감자결정) of the stock within ``MAX_LEAD`` before the day
hands the agreed split to ``capital_actions`` as a ``candidate``: the filed
reduction wins a day it confirms (042040 2026-06-26: filed 10:1, base moved
1/8), and the candidate is booked otherwise (042040 2026-04-30: a 10:1
merger two months before the reduction).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

from momentum.agent.market import KRX

from .dart_events import DisclosureEvent
from .fsc_adjustments import Adjustment

# An announcement older than this is not taken as the cause of a split day.
MAX_LEAD = timedelta(days=180)
RATIO_BASIS = (
    "fsc listed shares and exchange base price agree (FIN-02a); filing text not parsed"
)
BASE_RATIO_BASIS = (
    "exchange base price is a simple ratio; listed shares within 30% of it "
    "(user decision 2026-11-01); filing text not parsed"
)
SHARE_BAND = Decimal("0.30")
DEFERRED = "capital reduction filed; left to capital_actions"
_TICK = Decimal("0.005")
_MAX_DENOMINATOR = 10


@dataclass(frozen=True)
class Reconciliation:
    actions: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]


def _finite_decimal(ratio: Fraction) -> Decimal | None:
    denominator = ratio.denominator
    for prime in (2, 5):
        while denominator % prime == 0:
            denominator //= prime
    if denominator != 1:
        return None
    return Decimal(ratio.numerator) / Decimal(ratio.denominator)


def _base_ratio(factor: Decimal) -> Fraction | None:
    """The simple ratio the exchange base price moved by, within one tick (as FIN-02a)."""
    if factor <= 0:
        return None
    upward = factor if factor >= 1 else 1 / factor
    ratio = Fraction(str(upward)).limit_denominator(_MAX_DENOMINATOR)
    if ratio == 1:
        return None
    ratio = ratio if factor >= 1 else 1 / ratio
    target = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    return ratio if abs(factor - target) <= _TICK * target else None


def _other(item: Adjustment, prior: list[DisclosureEvent]) -> tuple[str, str | None]:
    """(reason, ratio) for an OTHER detection: a ratio only when the shares confirm the base price."""
    if not prior:
        return "other adjustment with no split filing", None
    ratio = _base_ratio(item.factor)
    if ratio is None:
        return "base price is not a simple ratio", None
    target = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    if item.share_ratio is None or abs(item.share_ratio - target) > SHARE_BAND * target:
        return "listed shares do not confirm the base-price ratio", None
    return "", str(ratio)


def reconcile(
    adjustments: Iterable[Adjustment],
    disclosures: Iterable[DisclosureEvent],
    reductions: Iterable[DisclosureEvent] = (),
) -> Reconciliation:
    by_code: dict[str, list[DisclosureEvent]] = {}
    for event in disclosures:
        if event.stock_code and event.report_kind in {"STOCK_SPLIT", "REVERSE_SPLIT"}:
            by_code.setdefault(event.stock_code, []).append(event)
    reduced_by: dict[str, list[DisclosureEvent]] = {}
    for event in reductions:
        if event.stock_code and event.report_kind == "CAPITAL_REDUCTION":
            reduced_by.setdefault(event.stock_code, []).append(event)
    actions, unresolved = [], []
    for item in adjustments:
        other = item.kind == "OTHER"
        if not other and (item.kind not in {"SPLIT_LIKE", "REVERSE_SPLIT_LIKE"} or item.ratio is None):
            continue
        if other:
            wanted = "STOCK_SPLIT" if item.factor > 1 else "REVERSE_SPLIT"
        else:
            wanted = "STOCK_SPLIT" if item.kind == "SPLIT_LIKE" else "REVERSE_SPLIT"
        opening = KRX.open_at(item.session)
        prior = sorted(
            (
                e
                for e in by_code.get(item.symbol, [])
                if e.report_kind == wanted
                and e.available_at <= opening
                and opening - e.available_at <= MAX_LEAD
            ),
            # The original filing announces first; corrections only restate it.
            key=lambda e: (e.is_correction, e.available_at, e.rcept_no),
        )
        base: dict[str, Any] = {
            "session": item.session.isoformat(),
            "symbol": item.symbol,
            "isin": item.isin,
            "kind": item.kind,
            "ratio": item.ratio,
        }
        basis, chosen = RATIO_BASIS, item.ratio
        if other:
            reason, agreed = _other(item, prior)
            if agreed is None:
                unresolved.append({**base, "reason": reason, "factor": str(item.factor),
                                   "share_ratio": None if item.share_ratio is None else str(item.share_ratio),
                                   "disclosures": [e.rcept_no for e in prior]})
                continue
            base["ratio"], basis, chosen = agreed, BASE_RATIO_BASIS, agreed
        assert chosen is not None
        ratio = _finite_decimal(Fraction(chosen))
        if not prior:
            later = [
                e.rcept_no
                for e in by_code.get(item.symbol, [])
                if e.report_kind == wanted
            ]
            reason = (
                "disclosure filed after the open" if later else "no matching disclosure"
            )
            unresolved.append({**base, "reason": reason, "disclosures": later})
            continue
        if ratio is None:
            unresolved.append({**base, "reason": "ratio is not a finite decimal"})
            continue
        source = prior[0]
        action = (
            {
                "security_id": item.symbol,
                "action_type": "split",
                "ex_date": item.session.isoformat(),
                "pay_date": None,
                "split_ratio": str(ratio),
                "cash_amount": None,
                "available_at": source.available_at.isoformat(),
                "evidence": {
                    "rcept_no": source.rcept_no,
                    "report_nm": source.report_nm,
                    "isin": item.isin,
                    "previous_close": str(item.previous_close),
                    "adjusted_base": str(item.adjusted_base),
                    "share_ratio": str(item.share_ratio),
                    "ratio_basis": basis,
                },
            }
        )
        if other and any(e.available_at <= opening and opening - e.available_at <= MAX_LEAD
                         for e in reduced_by.get(item.symbol, [])):
            # capital_actions books it only when no filed reduction confirms this day
            unresolved.append({**base, "reason": DEFERRED, "candidate": action})
            continue
        actions.append(action)
    return _same_issuer_classes(actions, unresolved)


def _issuer(isin: str) -> str | None:
    # KR ISINs: KR + 1-digit security type + 5-digit issuer code + class...
    # (BYC 2024: common KR7001460005, preferred KR7001461003 share 00146).
    return isin[3:8] if len(isin) == 12 and isin.startswith("KR") else None


def _same_issuer_classes(
    actions: list[dict[str, Any]], unresolved: list[dict[str, Any]]
) -> Reconciliation:
    """Extend a reconciled split to the issuer's other share classes.

    Filings name only the common share's code, so a preferred share split on
    the same day by the same detected ratio has no filing of its own. It
    inherits the common share's action (and its availability) only when the
    issuer code, ex-date and ratio all agree; anything else stays unresolved.
    """
    remaining = []
    for item in unresolved:
        issuer = _issuer(item["isin"])
        match = None
        if item["reason"] == "no matching disclosure" and issuer:
            match = next(
                (
                    a
                    for a in actions
                    if _issuer(a["evidence"]["isin"]) == issuer
                    and a["evidence"]["isin"] != item["isin"]
                    and a["ex_date"] == item["session"]
                    and Fraction(a["split_ratio"]) == Fraction(item["ratio"])
                ),
                None,
            )
        if match is None:
            remaining.append(item)
            continue
        actions.append(
            {
                **match,
                "security_id": item["symbol"],
                "evidence": {
                    **match["evidence"],
                    "isin": item["isin"],
                    "via_share_class": match["security_id"],
                },
            }
        )
    return Reconciliation(actions, remaining)
