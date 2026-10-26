"""Point-in-time annual financials and listed share counts (explore v7, user decision 2026-10-26).

Research-only inputs for the fundamental rankings and the market-cap universe.
A record is visible only once its own publication time has passed; nothing is
filled in. A later filing for the same fiscal period (a correction) replaces
the earlier one only from its own publication time.
"""

from __future__ import annotations

import csv
import json
import re
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .contracts import AgentError, D, aware, dec, digest, timestamp

STATEMENTS = ("CFS", "OFS")
# Preregistered (explore v7 §2): an annual report older than this many months
# after its fiscal year end is no longer used.
MAX_FINANCIAL_AGE_MONTHS = 15


def months_after(day: date, months: int) -> date:
    """``day`` plus calendar months, clamped to the target month's last day."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", value):
        raise AgentError("Invalid security symbol")


@dataclass(frozen=True)
class FinancialRecord:
    symbol: str
    period_end: date
    available_at: datetime
    statement: str
    equity: D | None
    assets: D | None
    operating_income: D | None
    source: str

    def __post_init__(self):
        _symbol(self.symbol)
        object.__setattr__(self, "available_at", aware(self.available_at))
        if type(self.period_end) is not date or self.available_at.date() <= self.period_end:
            raise AgentError("A financial report cannot be public before its period ends")
        if self.statement not in STATEMENTS or not isinstance(self.source, str) or not self.source.strip():
            raise AgentError("Explicit statement kind and source are required")
        for name in ("equity", "assets", "operating_income"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, dec(value))


@dataclass(frozen=True)
class ShareCount:
    """One run of identical daily listed-share counts, ``session`` through ``through``.

    The daily quote files report the count on every session a stock is
    quoted; storing runs keeps the research input small. The builder must
    split a run wherever a quoted session lacks a count, so a lookup never
    carries a value over a day that had none. ``available_at`` is the first
    session's publication; later days in the run repeat that value.
    """

    symbol: str
    session: date
    through: date
    available_at: datetime
    listed_shares: int
    source: str

    def __post_init__(self):
        _symbol(self.symbol)
        object.__setattr__(self, "available_at", aware(self.available_at))
        if (type(self.session) is not date or type(self.through) is not date or self.through < self.session
                or type(self.listed_shares) is not int or self.listed_shares <= 0):
            raise AgentError("Listed shares must be a positive integer over an ordered session run")
        if not isinstance(self.source, str) or not self.source.strip():
            raise AgentError("Explicit share count source is required")


class FundamentalHistory:
    def __init__(self, financials: list[FinancialRecord], shares: list[ShareCount]):
        self.financial_records = tuple(sorted(financials, key=lambda r: (r.symbol, r.period_end, r.available_at)))
        self.share_records = tuple(sorted(shares, key=lambda r: (r.symbol, r.session)))
        if len({(r.symbol, r.period_end, r.available_at) for r in financials}) != len(financials):
            raise AgentError("Duplicate financial record")
        self._shares: dict[str, list[ShareCount]] = {}
        for record in self.share_records:
            runs = self._shares.setdefault(record.symbol, [])
            if runs and runs[-1].through >= record.session:
                raise AgentError("Overlapping share count runs")
            runs.append(record)
        self._starts = {symbol: [r.session for r in runs] for symbol, runs in self._shares.items()}
        self._financials: dict[str, list[FinancialRecord]] = {}
        for record in self.financial_records:
            self._financials.setdefault(record.symbol, []).append(record)
        self._id: str | None = None

    @property
    def id(self) -> str:
        # Computed on first use: research prefixes build many histories whose
        # id is only needed for fingerprints and audited screens.
        if self._id is None:
            self._id = digest({"financials": self.financial_records, "shares": self.share_records})
        return self._id

    def until(self, end: date, at: datetime) -> FundamentalHistory:
        """Records published by ``at``, with share runs cut at ``end``, so a
        prefix's id does not change when later sessions extend a run."""
        at = aware(at)
        return FundamentalHistory(
            [r for r in self.financial_records if r.available_at <= at],
            [replace(r, through=min(r.through, end)) for r in self.share_records
             if r.available_at <= at and r.session <= end])

    def financials(self, symbol: str, at: datetime) -> FinancialRecord | None:
        """Latest fiscal period published by ``at``; its latest published version."""
        at = aware(at)
        visible = [r for r in self._financials.get(symbol, ()) if r.available_at <= at]
        return max(visible, key=lambda r: (r.period_end, r.available_at)) if visible else None

    def listed_shares(self, symbol: str, session: date, at: datetime) -> int | None:
        index = bisect_right(self._starts.get(symbol, ()), session) - 1
        if index < 0:
            return None
        record = self._shares[symbol][index]
        return record.listed_shares if session <= record.through and record.available_at <= aware(at) else None


def read_fundamentals(financials_path: Path, shares_path: Path) -> FundamentalHistory:
    financials = []
    with financials_path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                raw["period_end"] = date.fromisoformat(raw["period_end"])
                raw["available_at"] = timestamp(raw["available_at"])
                financials.append(FinancialRecord(**raw))
            except (KeyError, TypeError, ValueError, AttributeError):
                raise AgentError(f"Invalid financial JSONL record at line {number}") from None
    shares = []
    with shares_path.open(newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            try:
                shares.append(ShareCount(row["symbol"], date.fromisoformat(row["session"]),
                                         date.fromisoformat(row["through"]), timestamp(row["available_at"]),
                                         int(row["listed_shares"]), row["source"]))
            except (KeyError, TypeError, ValueError):
                raise AgentError(f"Invalid share count CSV row at line {number}") from None
    return FundamentalHistory(financials, shares)
