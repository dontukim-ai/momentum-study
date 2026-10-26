"""Listed-share runs from saved FSC daily stock quotes (explore v7 §3, RES-05c).

The daily quote (``lstgStCnt``) is the share count of every stock quoted that
session, public with the bar (``KRX.historical_bar_at``). kis-agent stores it as
runs of identical counts over consecutive sessions. A run ends when the count
changes, when a quoted row has no usable count, or when the stock is absent
for a session, so no value is ever carried over a session without one.
Output CSV columns: symbol, session, through, available_at, listed_shares, source.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

from .fsc_adjustments import _rows

SOURCE = "fsc-stock-price:lstgStCnt"


@dataclass
class _Run:
    start: int
    last: int
    shares: int


def share_runs(
    fsc_root: Path, sessions: Sequence[date]
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    runs: list[dict[str, str]] = []
    open_runs: dict[str, _Run] = {}
    report: dict[str, Any] = {"sessions": len(sessions), "rows": 0, "missing_count_rows": 0, "runs": 0}

    def close(symbol: str) -> None:
        run = open_runs.pop(symbol)
        runs.append({
            "symbol": symbol,
            "session": sessions[run.start].isoformat(),
            "through": sessions[run.last].isoformat(),
            "available_at": KRX.historical_bar_at(sessions[run.start]).isoformat(),
            "listed_shares": str(run.shares),
            "source": SOURCE,
        })

    for index, day in enumerate(sessions):
        rows = _rows(Path(fsc_root) / "stock" / day.isoformat(), day)
        if rows is None:
            raise SpecUndefinedError(f"no saved FSC stock quotes for session {day}")
        seen: set[str] = set()
        for row in rows:
            symbol = str(row.get("srtnCd") or "").strip()
            if not symbol or symbol in seen:
                raise SpecUndefinedError(f"{day}: missing or repeated srtnCd {symbol!r}")
            seen.add(symbol)
            report["rows"] += 1
            text = str(row.get("lstgStCnt") or "").strip()
            shares = int(text) if re.fullmatch(r"\d+", text) and int(text) > 0 else None
            run = open_runs.get(symbol)
            if run is not None and (shares != run.shares or run.last != index - 1):
                close(symbol)
                run = None
            if shares is None:
                report["missing_count_rows"] += 1
            elif run is None:
                open_runs[symbol] = _Run(index, index, shares)
            else:
                run.last = index
        for symbol in [s for s, r in open_runs.items() if r.last != index]:
            close(symbol)
    for symbol in list(open_runs):
        close(symbol)
    runs.sort(key=lambda r: (r["symbol"], r["session"]))
    report["runs"] = len(runs)
    return runs, report


def write_runs(runs: Sequence[dict[str, str]], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["symbol", "session", "through", "available_at", "listed_shares", "source"]
        )
        writer.writeheader()
        writer.writerows(runs)
