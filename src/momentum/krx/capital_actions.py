"""Bonus-issue and capital-reduction actions from OpenDART details (FIN-02m).

무상증자결정 and 감자결정 are 주요사항보고서 (``pblntf_ty=B``) with structured
detail APIs (``fricDecsn``, ``crDecsn``; real answers checked 2026-10-20).
Their dates read "2024년 04월 17일", numbers carry commas and an absent value
is "-". ``nstk_ascnt_ps_ostk`` is the number of *additional* shares per old
share, so a bonus issue moves the exchange base price by ``1 + ratio``.

An action needs both a filing public before the event's open and the price
adjustment the exchange actually applied (FIN-02a detections); a filing whose
ratio the price does not confirm stays unresolved (2024: SNT에너지 announced
2 bonus shares per share, the base moved by 2.755). Policy B (user decision,
2026-10-20) is applied by the ledger:

- bonus issue: ex-date is the session before the last session on or before
  the allotment record date (T+2, as for dividends; 스튜디오미르 2024: record
  04-08, ex 04-05, factor 5.993 for 5 bonus shares); the new shares list on
  ``nstk_lstprd``, taken from the latest filing (corrections restate it);
- capital reduction: the relisting session is where the *listed share
  count* falls by the remaining fraction ``1 - cr_rt_ostk / 100`` (within
  the same 0.5%) and the base price moves down. The price is not required
  to match the fraction: the relisting base is set by an opening procedure,
  and 2023-10~2024-06 relistings sat 2-24% away from it (003060 expected
  0.1, base moved 0.1045; 031860 0.10 -> 0.0762). Treasury-share
  cancellations move no price and produce nothing; paid reductions (cash
  refunds) are not supported and stay unresolved.
  User decision 2026-11-01: the share count also drifts on a relisting day
  (odd lots, conversions; 024810 2021 0.1 -> 0.1035, 139050 2023 0.0667 ->
  0.0738), so a day whose share ratio and base-price move both lie within
  ``BAND`` (30%) of the filed fraction confirms it too; the filed fraction
  is what the ledger applies.

Subsidiary filings and filings without a listed stock code are ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .dart_dividends import ex_dividend_date
from .dart_events import DisclosureEvent

TOLERANCE = Decimal("0.005")
BAND = Decimal("0.30")
# A reduction relists weeks after its record date (2024: 비트플래닛 record
# 04-29, relisting planned 05-22); a price move later than this is not taken
# as its effect.
MAX_RELISTING_LAG = timedelta(days=120)
KINDS = {"BONUS_ISSUE": "fricDecsn", "CAPITAL_REDUCTION": "crDecsn"}


@dataclass(frozen=True)
class CapitalActions:
    actions: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def korean_day(text: str | None) -> date | None:
    if text is None or text.strip() in {"", "-"}:
        return None
    match = re.fullmatch(
        r"\s*(\d{4})\s*[년.-]\s*(\d{1,2})\s*[월.-]\s*(\d{1,2})\s*일?\s*", text
    )
    if match is None:
        raise SpecUndefinedError(f"unreadable date: {text!r}")
    return date(*(int(part) for part in match.groups()))


def number(text: str | None) -> Decimal | None:
    if text is None or text.strip() in {"", "-"}:
        return None
    try:
        value = Decimal(text.replace(",", "").strip())
    except InvalidOperation as exc:
        raise SpecUndefinedError(f"unreadable number: {text!r}") from exc
    if not value.is_finite():
        raise SpecUndefinedError(f"unreadable number: {text!r}")
    return value


def relevant(events: Iterable[DisclosureEvent]) -> list[DisclosureEvent]:
    return [
        e
        for e in events
        if e.report_kind in KINDS and e.stock_code and "자회사" not in e.report_nm
    ]


def load_details(root: Path) -> dict[str, dict[str, Any]]:
    """Every saved detail item, keyed by the item's own rcept_no.

    The detail APIs select by *first* reception date and answer with the
    latest version of a filing under that version's own number (2024:
    프로브잇's 2023-12-27 filing came back as its 2024-01-12 correction).
    So an item is attributed only to the filing whose number it carries,
    whichever request saved it; an earlier version whose values were
    replaced never gets them.
    """
    found: dict[str, dict[str, Any]] = {}
    for api in KINDS.values():
        for path in sorted((Path(root) / "dart_detail" / api).glob("*.json")):
            for item in json.loads(path.read_text(encoding="utf-8")).get("list") or []:
                number_ = str(item.get("rcept_no") or "")
                if number_ in found and found[number_] != item:
                    raise SpecUndefinedError(f"{path}: conflicting item {number_}")
                found[number_] = dict(item)
    return found


def _close(value: Decimal, target: Decimal) -> bool:
    return abs(value - target) <= TOLERANCE * target


def build(
    events: Iterable[DisclosureEvent],
    details: Mapping[str, dict[str, Any]],
    adjustments: Iterable[Mapping[str, Any]],
    sessions: tuple[date, ...],
    confirmed_through: date,
    existing: Iterable[Mapping[str, Any]] = (),
    candidates: Iterable[dict[str, Any]] = (),
) -> CapitalActions:
    """``details`` maps rcept_no to its detail item; ``existing`` are actions
    already made elsewhere (splits), whose day a reduction must not repeat;
    ``candidates`` are base-ratio splits split_actions deferred because a
    reduction was filed: booked unless a reduction here confirms their day."""
    moves: dict[tuple[str, date], Decimal] = {}
    shares: dict[tuple[str, date], Decimal | None] = {}
    for item in adjustments:
        seen = (str(item["symbol"]), date.fromisoformat(str(item["session"])))
        moves[seen] = Decimal(str(item["factor"]))
        ratio = item.get("share_ratio")
        shares[seen] = None if ratio in (None, "None") else Decimal(str(ratio))
    taken = {(str(a["security_id"]), str(a["ex_date"])) for a in existing}
    actions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    groups: dict[
        tuple[str, str, str], list[tuple[DisclosureEvent, dict[str, Any]]]
    ] = {}
    for event in relevant(events):
        base: dict[str, Any] = {
            "rcept_no": event.rcept_no,
            "stock_code": event.stock_code,
        }
        detail = details.get(event.rcept_no)
        if detail is None:
            if "정" in event.remarks or event.is_correction:
                # A later correction replaced this version's values (rm "정"),
                # or a correction is only answered under its original's date.
                skipped.append({**base, "reason": "values only in another version"})
            else:
                unresolved.append({**base, "reason": "detail answer not collected"})
            continue
        try:
            key_day = (
                korean_day(detail.get("nstk_asstd"))
                if event.report_kind == "BONUS_ISSUE"
                else korean_day(detail.get("cr_std"))
            )
        except SpecUndefinedError as exc:
            unresolved.append({**base, "reason": str(exc)})
            continue
        if key_day is None:
            unresolved.append({**base, "reason": "no record date"})
            continue
        assert event.stock_code is not None
        key = (event.report_kind, event.stock_code, key_day.isoformat())
        groups.setdefault(key, []).append((event, detail))
    for (kind, code, record_text), filings in sorted(groups.items()):
        filings.sort(key=lambda f: (f[0].available_at, f[0].rcept_no))
        base = {
            "stock_code": code,
            "kind": kind,
            "record_date": record_text,
            "filings": [f[0].rcept_no for f in filings],
        }
        result: dict[str, Any] | str | tuple[str, dict[str, Any]]
        try:
            if kind == "BONUS_ISSUE":
                result = _bonus(base, filings, moves, sessions, confirmed_through)
            else:
                result = _reduction(base, filings, moves, shares, confirmed_through)
        except SpecUndefinedError as exc:
            unresolved.append({**base, "reason": str(exc)})
            continue
        if isinstance(result, tuple):
            reason, observed = result
            (skipped if reason.startswith("skip:") else unresolved).append(
                {**base, "reason": reason.removeprefix("skip:"), **observed}
            )
            continue
        if isinstance(result, str):
            (skipped if result.startswith("skip:") else unresolved).append(
                {**base, "reason": result.removeprefix("skip:")}
            )
            continue
        if (result["security_id"], result["ex_date"]) in taken:
            skipped.append({**base, "reason": "same day already has a split action"})
            continue
        actions.append(result)
    actions, dropped = _one_per_day(actions)
    unresolved += [d for d in dropped if d["reason"] != _SAME_DAY_KEPT]
    skipped += [d for d in dropped if d["reason"] == _SAME_DAY_KEPT]
    reduced_days = {(a["security_id"], a["ex_date"]) for a in actions if a["action_type"] == "capital_reduction"}
    for c in candidates:
        if (c["security_id"], c["ex_date"]) in reduced_days:
            skipped.append({"stock_code": c["security_id"], "ex_date": c["ex_date"], "split_ratio": c["split_ratio"],
                            "reason": "base-ratio split replaced by the filed reduction"})
        else:
            actions.append({**c, "evidence": {**c["evidence"], "deferred_from": "split_actions"}})
    return CapitalActions(actions, unresolved, skipped)


_STRICT = "listed shares within 0.5%"
_SAME_DAY_KEPT = "relisting day already confirmed by another filing"


def _one_per_day(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """At most one reduction per (stock, relisting day).

    With the 30% band two reductions (different record dates) can confirm the
    same day. An exact (0.5%) match keeps its day; the same fraction filed
    twice keeps the earlier filing; different fractions with no exact match
    are all unresolved, since the ledger cannot apply both.
    """
    days: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in actions:
        if a["action_type"] == "capital_reduction":
            days.setdefault((a["security_id"], a["ex_date"]), []).append(a)
    drop: dict[int, str] = {}
    for same in days.values():
        if len(same) < 2:
            continue
        exact = [a for a in same if a["evidence"].get("confirmed_by") == _STRICT]
        fractions = {Decimal(a["split_ratio"]) for a in same}
        if len(exact) == 1:
            keep = exact[0]
        elif not exact and len(fractions) == 1:
            keep = min(same, key=lambda a: (a["available_at"], a["evidence"]["rcept_no"]))
        else:
            keep = None
        for a in same:
            if a is not keep:
                drop[id(a)] = _SAME_DAY_KEPT if keep is not None else "several filings confirm the same relisting day"
    kept = [a for a in actions if id(a) not in drop]
    dropped = [{"stock_code": a["security_id"], "rcept_no": a["evidence"]["rcept_no"], "ex_date": a["ex_date"],
                "split_ratio": a["split_ratio"], "reason": drop[id(a)]} for a in actions if id(a) in drop]
    return kept, dropped


def _source(
    filings: list[tuple[DisclosureEvent, dict[str, Any]]], ex: date
) -> tuple[DisclosureEvent, dict[str, Any]] | None:
    known = [f for f in filings if f[0].available_at <= KRX.open_at(ex)]
    return known[-1] if known else None


def _bonus(
    base: dict[str, Any],
    filings: list[tuple[DisclosureEvent, dict[str, Any]]],
    moves: Mapping[tuple[str, date], Decimal],
    sessions: tuple[date, ...],
    confirmed_through: date,
) -> dict[str, Any] | str:
    record = date.fromisoformat(base["record_date"])
    ex = ex_dividend_date(record, sessions, confirmed_through)
    source = _source(filings, ex)
    if source is None:
        return "filed after the ex-date open"
    ratio = number(source[1].get("nstk_ascnt_ps_ostk"))
    if ratio is None or ratio <= 0:
        return "no common-share allotment ratio"
    if any(number(d.get("nstk_ascnt_ps_ostk")) != ratio for _, d in filings):
        return "allotment ratio changed between filings"
    listing = korean_day(filings[-1][1].get("nstk_lstprd"))
    if listing is None or listing <= ex:
        return f"listing date {listing} not after ex-date {ex}"
    factor = moves.get((base["stock_code"], ex))
    # Treasury shares receive no bonus shares, so the base price moves by the
    # whole-company growth 1 + new shares / shares before, slightly below
    # 1 + the per-share allotment holders receive (2020-2024: 62 of 64
    # filings unconfirmed against 1 + ratio matched this within 0.5%).
    new = number(source[1].get("nstk_ostk_cnt"))
    before = number(source[1].get("bfic_tisstk_ostk"))
    effective = 1 + new / before if new and before else None
    if factor is not None and _close(factor, 1 + ratio):
        basis = "1 + allotment"
    elif factor is not None and effective is not None and _close(factor, effective):
        basis = "1 + new shares / shares before"
    else:
        return f"price adjustment {factor} on {ex} does not confirm 1 + {ratio}"
    return {
        "security_id": base["stock_code"],
        "action_type": "bonus_issue",
        "ex_date": ex.isoformat(),
        "pay_date": listing.isoformat(),
        "split_ratio": str(ratio),
        "cash_amount": None,
        "available_at": source[0].available_at.isoformat(),
        "evidence": {
            "rcept_no": source[0].rcept_no,
            "record_date": base["record_date"],
            "listing_rcept_no": filings[-1][0].rcept_no,
            "price_factor": str(factor),
            "confirmed_by": basis,
            "filings": base["filings"],
            "class_share_ratio_ignored": source[1].get("nstk_ascnt_ps_iostk"),
        },
    }


def _reduction(
    base: dict[str, Any],
    filings: list[tuple[DisclosureEvent, dict[str, Any]]],
    moves: Mapping[tuple[str, date], Decimal],
    shares: Mapping[tuple[str, date], Decimal | None],
    confirmed_through: date,
) -> dict[str, Any] | str | tuple[str, dict[str, Any]]:
    record = date.fromisoformat(base["record_date"])
    latest = filings[-1][1]
    method = str(latest.get("cr_mth") or "")
    if "유상" in method:
        return "paid capital reduction is not supported"
    if "소각" in method and ("자기" in method or "자사주" in method):
        # Cancelling the company's own shares leaves every holder's count.
        return "skip:treasury share cancellation does not change holdings"
    percent = number(latest.get("cr_rt_ostk"))
    if percent is None or not 0 < percent < 100:
        return "skip:no common-share reduction"
    remaining = 1 - percent / 100
    code = base["stock_code"]
    window = sorted(
        day
        for (symbol, day) in moves
        if symbol == code and record <= day <= record + MAX_RELISTING_LAG
    )

    def strict(day: date) -> bool:
        ratio = shares.get((code, day))
        return ratio is not None and _close(ratio, remaining) and moves[(code, day)] < 1

    def banded(day: date) -> bool:
        ratio, move = shares.get((code, day)), moves[(code, day)]
        return (ratio is not None and abs(ratio - remaining) <= BAND * remaining
                and abs(move - remaining) <= BAND * remaining)

    def confirms(day: date) -> bool:
        return strict(day) or banded(day)

    # an exact share match is the relisting; the 30% band only when no session matches exactly
    candidates = [day for day in window if strict(day)] or [day for day in window if banded(day)]
    if not candidates:
        observed = {
            "expected_factor": str(remaining),
            "method": method,
            "planned_listing": latest.get("crsc_nstklstprd"),
            "observed": {
                d.isoformat(): f"price {moves[(code, d)]}, shares {shares.get((code, d))}"
                for d in window
            },
        }
        if window:
            return "price adjustment does not confirm the reduction", observed
        if record + MAX_RELISTING_LAG > confirmed_through:
            return "price data does not cover the relisting window", observed
        return "skip:no price adjustment observed", observed
    relisting = candidates[0]
    source = _source(filings, relisting)
    if source is None:
        return "filed after the relisting open"
    if any(number(d.get("cr_rt_ostk")) != percent for _, d in filings):
        return "reduction ratio changed between filings"
    return {
        "security_id": base["stock_code"],
        "action_type": "capital_reduction",
        "ex_date": relisting.isoformat(),
        "pay_date": None,
        "split_ratio": str(remaining),
        "cash_amount": None,
        "available_at": source[0].available_at.isoformat(),
        "evidence": {
            "rcept_no": source[0].rcept_no,
            "record_date": base["record_date"],
            "method": method,
            "planned_listing": latest.get("crsc_nstklstprd"),
            "price_factor": str(moves[(code, relisting)]),
            "share_ratio": str(shares[(code, relisting)]),
            "confirmed_by": _STRICT if strict(relisting)
            else "listed shares and base price within 30% (user decision 2026-11-01)",
            "filings": base["filings"],
        },
    }
