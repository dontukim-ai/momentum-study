"""069500 distributions from a hand-made file (v31 benchmark, user decision 2026-11-04).

The research inputs carry no ETF distributions: cash dividends come only from DART cash-dividend decisions,
which ETFs do not file, and KRX/KIND is not collected automatically. So the user writes the distribution
history from the manager's or KIND's pages into ``data/krx/kind/`` (never redistributed) as a CSV:

    record_date,pay_date,cash_per_unit,announced
    2024-04-30,2024-05-03,250,2024-04-25

Dates are ISO, the amount is won per unit without separators, ``announced`` is the day the amount was made
public. Each row becomes a ``cash_dividend`` of the research ``--actions`` format, by the stock rules:
- ex-date: the session before the last session on or before the record date (T+2, ``ex_dividend_date``);
- available at 18:00 KST on the announcement day (the disclosure rule); an amount not public before the
  ex-date open is refused, since the evaluation could not have booked it;
- a payment date that is not a session moves to the next session, the stated date stays in the evidence.
Anything unreadable, a duplicate record date or a payment before the record date stops the build.
"""

from __future__ import annotations

import csv
import json
import shutil
from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .dart_dividends import ex_dividend_date

COLUMNS = ["record_date", "pay_date", "cash_per_unit", "announced"]
KST = ZoneInfo("Asia/Seoul")                     # written like the DART actions: 18:00+09:00
RESEARCH_FILES = ("prices.csv", "securities.jsonl", "sessions.json")


def read_rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header != COLUMNS:
            raise SpecUndefinedError(f"{path}: header must be {','.join(COLUMNS)}, got {header}")
        out = []
        for n, fields in enumerate(reader, start=2):
            if not fields:
                continue
            if len(fields) != len(COLUMNS):
                raise SpecUndefinedError(f"{path}:{n}: expected {len(COLUMNS)} fields, got {len(fields)}")
            out.append(dict(zip(COLUMNS, (f.strip() for f in fields), strict=True)))
    return out


def _day(text: str, what: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SpecUndefinedError(f"unreadable {what}: {text!r}") from exc


def build(rows: Iterable[dict[str, str]], symbol: str, sessions: tuple[date, ...],
          confirmed_through: date) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[date] = set()
    for row in rows:
        record = _day(row["record_date"], "record_date")
        stated = _day(row["pay_date"], "pay_date")
        announced = _day(row["announced"], "announced")
        try:
            amount = Decimal(row["cash_per_unit"])
        except InvalidOperation as exc:
            raise SpecUndefinedError(f"unreadable cash_per_unit: {row['cash_per_unit']!r}") from exc
        if not amount.is_finite() or amount <= 0:
            raise SpecUndefinedError(f"{record}: cash_per_unit must be positive, got {amount}")
        if record in seen:
            raise SpecUndefinedError(f"duplicate record date {record}")
        seen.add(record)
        if stated < record:
            raise SpecUndefinedError(f"{record}: payment {stated} before the record date")
        ex = ex_dividend_date(record, sessions, confirmed_through)
        available = KRX.disclosure_at(announced)
        if available > KRX.open_at(ex):
            raise SpecUndefinedError(f"{record}: announced {announced}, not public before the ex-date {ex} open")
        later = [d for d in sessions if d >= stated]
        if not later or stated > confirmed_through:
            raise SpecUndefinedError(f"{record}: payment {stated} outside the confirmed calendar")
        actions.append({
            "security_id": symbol, "action_type": "cash_dividend", "ex_date": ex.isoformat(),
            "pay_date": later[0].isoformat(), "split_ratio": None, "cash_amount": str(amount),
            "available_at": available.astimezone(KST).isoformat(),
            "evidence": {"source": "manual distribution file", "record_date": record.isoformat(),
                         "pay_date_unrolled": stated.isoformat() if later[0] != stated else None},
        })
    return sorted(actions, key=lambda a: a["ex_date"])


def with_distributions(research: Path, actions: list[dict[str, Any]], out: Path) -> int:
    """A new research input: ``research`` with the distributions added to its actions. Never overwrites."""
    if out.exists():
        raise SpecUndefinedError(f"{out} exists; a research input is written once")
    base = json.loads((research / "actions.json").read_text(encoding="utf-8"))
    symbols = {a["security_id"] for a in actions}
    present = [a for a in base if a["security_id"] in symbols and a["action_type"] == "cash_dividend"]
    if present:
        raise SpecUndefinedError(f"{research} already has {len(present)} cash dividends of {sorted(symbols)}")
    merged = base + [{k: v for k, v in a.items() if k != "evidence"} for a in actions]
    merged.sort(key=lambda a: (a["ex_date"], a["security_id"], a["action_type"]))
    out.mkdir(parents=True)
    for name in RESEARCH_FILES:
        shutil.copy2(research / name, out / name)
    (out / "actions.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(actions)
