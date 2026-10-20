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


def reconcile(
    adjustments: Iterable[Adjustment], disclosures: Iterable[DisclosureEvent]
) -> Reconciliation:
    by_code: dict[str, list[DisclosureEvent]] = {}
    for event in disclosures:
        if event.stock_code and event.report_kind in {"STOCK_SPLIT", "REVERSE_SPLIT"}:
            by_code.setdefault(event.stock_code, []).append(event)
    actions, unresolved = [], []
    for item in adjustments:
        if item.kind not in {"SPLIT_LIKE", "REVERSE_SPLIT_LIKE"} or item.ratio is None:
            continue
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
        base = {
            "session": item.session.isoformat(),
            "symbol": item.symbol,
            "isin": item.isin,
            "kind": item.kind,
            "ratio": item.ratio,
        }
        ratio = _finite_decimal(Fraction(item.ratio))
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
        actions.append(
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
                    "ratio_basis": RATIO_BASIS,
                },
            }
        )
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
