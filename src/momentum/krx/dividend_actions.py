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
        if not known:
            unresolved.append({**base, "reason": "no filing before the ex-date open"})
            continue
        latest = known[-1]
        if any(
            f["per_share_common"] != latest["per_share_common"]
            for f in filings[len(known) :]
        ):
            unresolved.append(
                {**base, "reason": "amount changed after the ex-date open"}
            )
            continue
        if latest["per_share_common"] is None:
            unresolved.append({**base, "reason": "no common-share amount"})
            continue
        stated = _day(latest.get("pay_date"))
        anchor = _day(latest.get("agm_date")) or _day(latest.get("board_date"))
        if stated is not None:
            pay_date, basis = stated, "stated"
        elif anchor is not None:
            pay_date = add_month(anchor)
            basis = "agm+1m" if latest.get("agm_date") else "board+1m"
        else:
            unresolved.append({**base, "reason": "no payment date, AGM or board date"})
            continue
        if pay_date <= ex_date:
            unresolved.append(
                {
                    **base,
                    "reason": f"payment date {pay_date} not after ex-date {ex_date}",
                }
            )
            continue
        actions.append(
            {
                "security_id": code,
                "action_type": "cash_dividend",
                "ex_date": ex_date.isoformat(),
                "pay_date": pay_date.isoformat(),
                "split_ratio": None,
                "cash_amount": str(Decimal(latest["per_share_common"])),
                "available_at": latest["available_at"],
                "evidence": {
                    "rcept_no": latest["rcept_no"],
                    "record_date": record_text,
                    "dividend_kind": kind,
                    "pay_date_basis": basis,
                    "pay_date_policy": PAY_DATE_POLICY,
                    "filings": base["filings"],
                    # Class shares trade under their own code; not attached here.
                    "class_share_amount_ignored": latest["per_share_class"],
                },
            }
        )
    return DividendActions(actions, unresolved)
