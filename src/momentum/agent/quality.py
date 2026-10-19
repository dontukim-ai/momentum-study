"""Data quality codes and quarantine for research inputs (DATA-05a).

Two stages, because a single malformed supplier row must not reject a whole
file and must never be repaired by guessing:

1. ``screen_price_rows`` checks raw CSV rows *before* ``Bar`` construction and
   splits them into accepted rows and quarantined rows with reason codes. The
   source file is never modified; quarantined rows are kept verbatim.
2. ``assess`` checks a parsed ``ResearchInput`` across rows (abnormal returns,
   missing sessions, unknown security state). ``quarantine`` removes only the
   flagged bars. Missing bars are reported and never synthesized.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_agent.contracts import AgentError, session_time, timestamp
from trading_agent.data import CSV_FIELDS, Snapshot

from .contracts import ResearchInput

# KRX daily price limit is +/-30%. A move beyond it on consecutive bars cannot
# be a normal trading day, so it is quarantined unless a corporate action on
# that interval explains it. Exactly 30% is a legal limit move.
MAX_ABS_DAILY_RETURN = Decimal("0.30")

ROW_CODES = (
    "PARSE_ERROR",
    "NONPOSITIVE_PRICE",
    "OHLC_INCONSISTENT",
    "NEGATIVE_VOLUME",
    "AVAILABLE_BEFORE_CLOSE",
    "ADJUSTED_CLOSE_MISMATCH",
    "DUPLICATE_ROW",
)
QUARANTINE_CODES = (*ROW_CODES, "ABNORMAL_RETURN")
SEVERITY = {
    **{code: "error" for code in QUARANTINE_CODES},
    "MISSING_BAR": "warning",
    "UNKNOWN_SECURITY_STATE": "warning",
    "HALTED_NO_BAR": "info",
    "ACTION_EXPLAINED_RETURN": "info",
    "ZERO_VOLUME": "info",
}
_SYMBOL = re.compile(r"[A-Za-z0-9_]+")


class ScreenError(AgentError):
    """The file itself is unusable (for example, required columns are absent)."""


@dataclass(frozen=True)
class QuarantinedRow:
    line: int
    row: dict[str, str]
    codes: tuple[str, ...]


@dataclass(frozen=True)
class ScreenResult:
    accepted: list[dict[str, str]]
    quarantined: list[QuarantinedRow]

    def by_code(self) -> dict[str, int]:
        return dict(sorted(Counter(c for q in self.quarantined for c in q.codes).items()))


def _row_codes(row: Mapping[str, str]) -> tuple[tuple[str, ...], tuple[date, str] | None]:
    try:
        day = date.fromisoformat(row["session"])
        symbol = row["symbol"]
        if not symbol or not symbol.isascii() or not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol")
        prices = [Decimal(row[k]) for k in ("open", "high", "low", "close")]
        if any(not p.is_finite() for p in prices):
            raise ValueError("price")
        volume = int(row["volume"])
        available = timestamp(row["available_at"])
        adjusted = row.get("adjusted_close")
        adjusted_value = Decimal(adjusted) if adjusted else None
    except (KeyError, ValueError, TypeError, InvalidOperation, AgentError):
        return ("PARSE_ERROR",), None
    opening, high, low, close = prices
    codes = []
    if any(p <= 0 for p in prices):
        codes.append("NONPOSITIVE_PRICE")
    elif not low <= min(opening, close) <= max(opening, close) <= high:
        codes.append("OHLC_INCONSISTENT")
    if volume < 0:
        codes.append("NEGATIVE_VOLUME")
    if available < session_time(day, 15, 30):
        codes.append("AVAILABLE_BEFORE_CLOSE")
    if adjusted_value is not None and adjusted_value != close:
        codes.append("ADJUSTED_CLOSE_MISMATCH")
    return tuple(codes), (day, symbol)


def screen_price_rows(raw_rows: Iterable[Mapping[str, str]]) -> ScreenResult:
    """Split raw price rows; line numbers count the CSV header as line 1."""
    rows = [dict(r) for r in raw_rows]
    required = set(CSV_FIELDS[:-1])
    if rows and not required <= set(rows[0]):
        raise ScreenError(f"Price rows require columns: {', '.join(CSV_FIELDS[:-1])}")
    checked = []
    keys: Counter[tuple[date, str]] = Counter()
    for line, row in enumerate(rows, 2):
        codes, key = _row_codes(row)
        checked.append((line, row, codes, key))
        if not codes and key is not None:
            keys[key] += 1
    accepted, quarantined = [], []
    for line, row, codes, key in checked:
        if not codes and key is not None and keys[key] > 1:
            # Conflicting copies of one session/symbol: choosing either would
            # be a guess, so every copy is held back.
            codes = ("DUPLICATE_ROW",)
        if codes:
            quarantined.append(QuarantinedRow(line, row, codes))
        else:
            accepted.append(row)
    return ScreenResult(accepted, quarantined)


@dataclass(frozen=True)
class QualityIssue:
    code: str
    symbol: str
    session: date | None
    detail: dict[str, Any]

    @property
    def severity(self) -> str:
        return SEVERITY[self.code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "symbol": self.symbol,
            "session": self.session.isoformat() if self.session else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class QualityReport:
    issues: tuple[QualityIssue, ...]

    @property
    def quarantined(self) -> frozenset[tuple[str, date]]:
        return frozenset(
            (i.symbol, i.session)
            for i in self.issues
            if i.code in QUARANTINE_CODES and i.session is not None
        )

    def summary(self) -> dict[str, Any]:
        return {
            "issues": len(self.issues),
            "by_code": dict(sorted(Counter(i.code for i in self.issues).items())),
            "by_severity": dict(sorted(Counter(i.severity for i in self.issues).items())),
            "quarantined_bars": len(self.quarantined),
            "symbols_affected": sorted({i.symbol for i in self.issues}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "issues": [i.to_dict() for i in self.issues],
        }


def assess(
    data: ResearchInput, *, max_abs_return: Decimal = MAX_ABS_DAILY_RETURN
) -> QualityReport:
    issues: list[QualityIssue] = []
    by_symbol: dict[str, list[Any]] = {}
    for bar in data.snapshot.bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    actions: dict[str, list[date]] = {}
    for announced in data.actions:
        actions.setdefault(announced.action.security_id, []).append(
            announced.action.ex_date
        )
    states_at: dict[date, dict[str, Any]] = {}

    def states(day: date) -> dict[str, Any]:
        if day not in states_at:
            states_at[day] = data.securities.states(session_time(day, 16))
        return states_at[day]

    for symbol, bars in sorted(by_symbol.items()):
        bars.sort(key=lambda b: b.session)
        for bar in bars:
            if symbol not in states(bar.session):
                issues.append(QualityIssue("UNKNOWN_SECURITY_STATE", symbol, bar.session, {}))
            if bar.volume == 0:
                issues.append(QualityIssue("ZERO_VOLUME", symbol, bar.session, {}))
        # Compare with the last bar that was not quarantined. A one-day spike
        # then flags only the spike, while an unexplained permanent level shift
        # (for example a missing split) keeps flagging every later bar instead
        # of letting the jump re-enter returns as fake momentum.
        reference = bars[0]
        for current in bars[1:]:
            change = current.close / reference.close - 1
            if abs(change) <= max_abs_return:
                reference = current
                continue
            detail = {
                "reference_session": reference.session.isoformat(),
                "reference_close": str(reference.close),
                "close": str(current.close),
                "return": str(change),
            }
            explained = any(
                reference.session < ex <= current.session
                for ex in actions.get(symbol, ())
            )
            if explained:
                issues.append(
                    QualityIssue("ACTION_EXPLAINED_RETURN", symbol, current.session, detail)
                )
                reference = current
            else:
                issues.append(QualityIssue("ABNORMAL_RETURN", symbol, current.session, detail))
        present = {b.session for b in bars}
        first, last = bars[0].session, bars[-1].session
        for day in data.sessions:
            if day <= first or day in present:
                continue
            status = getattr(states(day).get(symbol), "status", None)
            if day > last and status != "NORMAL":
                # After the last bar only a still-listed NORMAL state proves a
                # gap; unknown, suspended or delisted names stay unreported.
                continue
            code = "HALTED_NO_BAR" if status == "SUSPENDED" else "MISSING_BAR"
            issues.append(QualityIssue(code, symbol, day, {"state": status}))
    issues.sort(key=lambda i: (i.session or date.min, i.symbol, i.code))
    return QualityReport(tuple(issues))


def quarantine(data: ResearchInput, report: QualityReport) -> ResearchInput:
    """Exclude flagged bars; everything else, including the clock, is kept."""
    held = report.quarantined
    bars = [b for b in data.snapshot.bars if (b.symbol, b.session) not in held]
    return replace(data, snapshot=Snapshot(bars, require_complete=False))
