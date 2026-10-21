"""Trading status per symbol inferred from collected FSC stock days (DATA-07a).

Only what the quotes show is inferred; nothing about 관리종목 or market
warnings, which come from manually downloaded KRX screens (data-sources §2.2):

- a row with a traded open or volume is ``NORMAL``;
- a no-trade row (open and volume both 0, close carried) is ``SUSPENDED``;
- a confirmed session without the symbol, after it had appeared, is also
  ``SUSPENDED`` when the symbol comes back later, and ``DELISTED`` from the
  first missing session when it never returns within the observed sessions.
  The end of the observed range cannot prove a delisting forever; it only
  says the symbol was gone for the rest of what was observed.

Each change becomes one event effective at that session's open and available
at that session's observation time (18:00 KST since LOOP-01b, the same moment
the historical bar becomes public). Tying the state to anything later than
the observation made the first session's prefix see no security at all
(first real run, 2026-10-20). Market comes from ``mrktCtg``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .fsc_adjustments import _decimal, _rows

MARKETS = {"KOSPI", "KOSDAQ", "KONEX"}


@dataclass(frozen=True)
class StatusEvent:
    symbol: str
    session: date
    status: str  # NORMAL, SUSPENDED, DELISTED
    market: str

    @property
    def effective_at(self) -> datetime:
        return KRX.open_at(self.session)

    @property
    def available_at(self) -> datetime:
        return KRX.observed_at(self.session)


def status_events(
    root: Path, sessions: tuple[date, ...], kind: str = "stock"
) -> list[StatusEvent]:
    """``sessions`` are confirmed observed sessions (tools/fsc_calendar.py)."""
    daily: dict[str, dict[date, tuple[str, str]]] = {}
    for day in sorted(sessions):
        rows = _rows(Path(root) / kind / day.isoformat(), day)
        if rows is None:
            raise SpecUndefinedError(f"session {day} is not collected")
        for row in rows:
            symbol = str(row.get("srtnCd") or "")
            if not symbol:
                continue
            market = str(row.get("mrktCtg") or "")
            if market not in MARKETS:
                raise SpecUndefinedError(f"{day} {symbol}: unknown market {market!r}")
            halted = _decimal(row.get("mkp")) == Decimal(0) and _decimal(
                row.get("trqu")
            ) == Decimal(0)
            daily.setdefault(symbol, {})[day] = (
                "SUSPENDED" if halted else "NORMAL",
                market,
            )
    ordered = sorted(sessions)
    events: list[StatusEvent] = []
    for symbol, seen in sorted(daily.items()):
        first, last = min(seen), max(seen)
        previous: tuple[str, str] | None = None
        market = seen[first][1]
        for day in ordered:
            if day < first:
                continue
            if day in seen:
                state = seen[day]
                market = state[1]
            elif day > last:
                state = ("DELISTED", market)
            else:
                state = ("SUSPENDED", market)
            if state != previous:
                events.append(StatusEvent(symbol, day, state[0], state[1]))
                previous = state
            if state[0] == "DELISTED":
                break
    return events
