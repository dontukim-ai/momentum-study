"""Liquidation trading and restructuring filings per stock, point in time (docs/blueprint/ma-cross-v20.md).

Built once from saved OpenDART list pages (types B and I) by ``tools/market_events.py``:

- ``liquidation``: a filing titled with 정리매매 (liquidation trading before a delisting). Liquidation
  trading has no daily price limit, so a jump of that stock within ``LIQUIDATION_DAYS`` after the
  filing date is a real price, not an unrecorded share change.
- ``restructuring``: 회사합병·회사분할·회사분할합병·주식교환/이전 decisions of the stock itself (a
  subsidiary's filing is not). The engine has no merger or spin-off action, so the stock is quarantined
  from the filing's availability for ``QUARANTINE_DAYS``: no new entry, and a holding is sold.

Rows are ``{symbol, kind, rcept_date, available_at, title}``; nothing is inferred beyond the title.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from momentum.config import SpecUndefinedError

LIQUIDATION = ("정리매매",)
RESTRUCTURING = ("회사합병결정", "회사분할결정", "회사분할합병결정", "주식교환", "주식이전")
SUBSIDIARY = ("종속회사", "자회사")
LIQUIDATION_DAYS = 30        # calendar days after the filing (정리매매 runs seven sessions)
QUARANTINE_DAYS = 365        # calendar days from availability (mergers and spin-offs close in months)


def classify(title: str) -> str | None:
    if any(k in title for k in LIQUIDATION):
        return "liquidation"
    if any(k in title for k in RESTRUCTURING) and not any(k in title for k in SUBSIDIARY):
        return "restructuring"
    return None


def build(filings: Iterable[Any]) -> list[dict[str, str]]:
    """One row per classified filing of a listed stock, sorted by symbol and availability."""
    rows = []
    for f in filings:
        kind = classify(f.report_nm)
        if kind is not None and f.stock_code:
            rows.append({"symbol": f.stock_code, "kind": kind, "rcept_date": f.rcept_date.isoformat(),
                         "available_at": f.available_at.isoformat(), "title": f.report_nm})
    rows.sort(key=lambda r: (r["symbol"], r["available_at"], r["title"]))
    return rows


def write(rows: Sequence[dict[str, str]], path: Path) -> None:
    if path.exists():
        raise SpecUndefinedError(f"{path} exists: market events are written once")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(rows), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


class MarketEvents:
    def __init__(self, rows: Iterable[dict[str, str]]):
        self._liquidation: dict[str, list[date]] = defaultdict(list)
        self._restructuring: dict[str, list[datetime]] = defaultdict(list)
        for r in rows:
            if r["kind"] == "liquidation":
                self._liquidation[r["symbol"]].append(date.fromisoformat(r["rcept_date"]))
            elif r["kind"] == "restructuring":
                self._restructuring[r["symbol"]].append(datetime.fromisoformat(r["available_at"]))
            else:
                raise SpecUndefinedError(f"unknown market event kind {r['kind']!r}")

    @classmethod
    def load(cls, path: Path) -> MarketEvents:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def liquidating(self, symbol: str, day: date) -> bool:
        """A liquidation-trading filing dated on or before ``day`` and at most LIQUIDATION_DAYS earlier."""
        return any(f <= day <= f + timedelta(days=LIQUIDATION_DAYS) for f in self._liquidation.get(symbol, ()))

    def quarantined(self, symbol: str, at: datetime) -> bool:
        """A restructuring filing available at ``at`` and at most QUARANTINE_DAYS old."""
        return any(a <= at <= a + timedelta(days=QUARANTINE_DAYS) for a in self._restructuring.get(symbol, ()))
