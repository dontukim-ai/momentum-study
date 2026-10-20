"""Cash-dividend corporate actions from parsed decisions (FIN-02e).

Policy A (user decision, 2026-10-20) for a missing payment date: when the
filing leaves 배당금지급 예정일자 as "-", payment is placed one month after
the shareholders' meeting date it states, or else one month after the board
decision. That is the latest date the law and the filings' own wording
("주총일로부터 1개월 이내") allow, so cash is never treated as usable early;
the dividend itself is already in NAV as a receivable from the ex-date.
With neither date the decision produces no action.

Other rules, each refusing rather than guessing:
- subsidiary filings, non-cash payments and class-share amounts are not
  attached to the filer's common stock;
- the amount and availability come from the latest filing published before
  the ex-date open; a later filing that changes the amount is reported,
  because the evaluation could not have known it;
- the ex-date needs an observed calendar confirmed through the record date.
"""

from __future__ import annotations

import calendar as month_calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .dart_dividends import ex_dividend_date

PAY_DATE_POLICY = "A: stated date, else AGM + 1 month, else board decision + 1 month"


@dataclass(frozen=True)
class DividendActions:
    actions: list[dict[str, Any]]
    unresolved: list[dict[str, Any]]


def add_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, month_calendar.monthrange(year, month)[1]))


def _day(value: str | None) -> date | None:
    return None if value is None else date.fromisoformat(value)


def _payment_date(source: dict[str, Any]) -> tuple[date, str] | None:
    stated = _day(source.get("pay_date"))
    if stated is not None:
        return stated, "stated"
    if source.get("agm_date"):
        return add_month(date.fromisoformat(source["agm_date"])), "agm+1m"
    if source.get("board_date"):
        return add_month(date.fromisoformat(source["board_date"])), "board+1m"
    return None


def build(
    records: Iterable[dict[str, Any]],
    sessions: tuple[date, ...],
    confirmed_through: date,
) -> DividendActions:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    for record in records:
        base = {"rcept_no": record["rcept_no"], "stock_code": record["stock_code"]}
        if record.get("subsidiary"):
            unresolved.append(
                {**base, "reason": "subsidiary filing; payer has no stock code"}
            )
        elif "현금" not in record["payment_kind"]:
            unresolved.append(
                {**base, "reason": f"non-cash payment: {record['payment_kind']}"}
            )
        elif not record.get("stock_code"):
            unresolved.append({**base, "reason": "no stock code"})
        elif record.get("record_date") is None:
            unresolved.append(
                {**base, "reason": "record date set by a separate filing"}
            )
        else:
            key = (record["stock_code"], record["record_date"], record["dividend_kind"])
            groups.setdefault(key, []).append(record)
    actions = []
    for (code, record_text, kind), filings in sorted(groups.items()):
        filings.sort(key=lambda r: (r["available_at"], r["rcept_no"]))
        base = {
            "stock_code": code,
            "record_date": record_text,
            "dividend_kind": kind,
            "filings": [f["rcept_no"] for f in filings],
        }
        try:
            ex_date = ex_dividend_date(
                date.fromisoformat(record_text), sessions, confirmed_through
            )
        except SpecUndefinedError as exc:
            unresolved.append({**base, "reason": str(exc)})
            continue
        opening = KRX.open_at(ex_date)
        known = [
            f for f in filings if datetime.fromisoformat(f["available_at"]) <= opening
        ]
        if known:
            # On time: the amount was public before the ex-date open.
            source, recognized_on, after = known[-1], None, filings[len(known) :]
            changed = "amount changed after the ex-date open"
        else:
            # Late (pre-2023 practice): booked on the first session after the
            # earliest filing, from that filing's amount (policy A, 2026-10-20).
            source, after = filings[0], filings[1:]
            published = datetime.fromisoformat(source["available_at"])
            recognized_on = next(
                (d for d in sorted(sessions) if KRX.open_at(d) >= published), None
            )
            if recognized_on is None or recognized_on > confirmed_through:
                unresolved.append(
                    {**base, "reason": "calendar does not reach the recognition day"}
                )
                continue
            changed = "amount changed after recognition"
        if any(f["per_share_common"] != source["per_share_common"] for f in after):
            unresolved.append({**base, "reason": changed})
            continue
        if source["per_share_common"] is None:
            unresolved.append({**base, "reason": "no common-share amount"})
            continue
        payment = _payment_date(source)
        if payment is None:
            unresolved.append({**base, "reason": "no payment date, AGM or board date"})
            continue
        pay_date, basis = payment
        floor, label = (
            (ex_date, "ex-date")
            if recognized_on is None
            else (recognized_on, "recognition day")
        )
        if pay_date <= floor:
            unresolved.append(
                {**base, "reason": f"payment date {pay_date} not after {label} {floor}"}
            )
            continue
        action: dict[str, Any] = {
            "security_id": code,
            "action_type": "cash_dividend"
            if recognized_on is None
            else "late_cash_dividend",
            "ex_date": ex_date.isoformat(),
            "pay_date": pay_date.isoformat(),
            "split_ratio": None,
            "cash_amount": str(Decimal(source["per_share_common"])),
            "available_at": source["available_at"],
            "evidence": {
                "rcept_no": source["rcept_no"],
                "record_date": record_text,
                "dividend_kind": kind,
                "pay_date_basis": basis,
                "pay_date_policy": PAY_DATE_POLICY,
                "filings": base["filings"],
                # Class shares trade under their own code; not attached here.
                "class_share_amount_ignored": source["per_share_class"],
            },
        }
        if recognized_on is not None:
            action["recognized_on"] = recognized_on.isoformat()
        actions.append(action)
    return DividendActions(actions, unresolved)
