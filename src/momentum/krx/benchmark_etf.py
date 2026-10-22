"""Price rows and security states for one preregistered market ETF (RES-01b).

The research input so far held only FSC stock quotes, so the opt-in
``market_etf`` benchmark had nothing to read and the equal-weight benchmark
rounded to cash at 1,000,000 KRW (explore v2d, 2026-10-23). This builds the
missing pieces for exactly one ETF from saved FSC ETF quote pages:

- price rows, converted and row-screened like stock quotes (accepted rows only;
  quarantined rows are reported, never repaired);
- status events from the same quote rules as stocks, with ``asset_type`` ETF
  and the market declared as KOSPI (every KRX ETF is listed there; ETF quotes
  carry no ``mrktCtg``);
- ``leveraged``/``inverse`` false only because every quote of the symbol names
  the index the operator expected (``bssIdxIdxNm``); a different or missing
  index name stops the build instead of being guessed from the product name.

ETF distributions are not in the corporate-action input, so the benchmark's
raw-price return omits them (data-sources §2, reported in ``coverage``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.agent.quality import screen_price_rows
from momentum.config import SpecUndefinedError

from .fsc_adjustments import _rows
from .fsc_prices import convert_pages
from .fsc_states import status_events

KIND = "etf"
MARKET = "KOSPI"
SOURCE = "fsc-etf-quotes status; index {index} checked on every quote (RES-01b)"


def _pages(root: Path, day: date) -> list[Path]:
    folder = Path(root) / KIND / day.isoformat()
    if _rows(folder, day) is None:
        raise SpecUndefinedError(f"ETF session {day} is not collected")
    return sorted(folder.glob("page-*.json"))


def build_benchmark_etf(
    root: Path, sessions: tuple[date, ...], symbol: str, expected_index: str
) -> tuple[list[dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    if not sessions:
        raise SpecUndefinedError("no sessions")
    if not expected_index.strip():
        raise SpecUndefinedError("expected index name is required")
    indexes: dict[str, int] = {}
    for day in sessions:
        for row in _rows(Path(root) / KIND / day.isoformat(), day) or []:
            if str(row.get("srtnCd") or "") == symbol:
                name = str(row.get("bssIdxIdxNm") or "")
                indexes[name] = indexes.get(name, 0) + 1
    if not indexes:
        raise SpecUndefinedError(f"{symbol} has no ETF quote in the sessions")
    if set(indexes) != {expected_index}:
        raise SpecUndefinedError(
            f"{symbol} index names {sorted(indexes)} differ from {expected_index!r}"
        )
    pages = [path for day in sessions for path in _pages(root, day)]
    converted = convert_pages(pages, KIND)
    screen = screen_price_rows(r for r in converted.rows if r["symbol"] == symbol)
    events = status_events(
        root, sessions, KIND, market=MARKET, symbols=frozenset({symbol})
    )
    source = SOURCE.format(index=expected_index)
    records = [
        {
            "symbol": e.symbol,
            "effective_at": KRX.open_at(e.session).isoformat(),
            "available_at": KRX.observed_at(e.session).isoformat(),
            "status": e.status,
            "asset_type": "ETF",
            "market": e.market,
            "managed": False,
            "caution": False,
            "leveraged": False,
            "inverse": False,
            "bid": None,
            "ask": None,
            "source": source,
        }
        for e in events
    ]
    report = {
        "symbol": symbol,
        "index": expected_index,
        "quotes": sum(indexes.values()),
        "accepted": len(screen.accepted),
        "quarantined": screen.by_code(),
        "no_trade": sum(1 for r in converted.no_trade if r.get("symbol") == symbol),
        "status_events": [(e.session.isoformat(), e.status) for e in events],
        "coverage": "raw prices; ETF distributions not in corporate actions",
    }
    return screen.accepted, records, report
