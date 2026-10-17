"""Immutable, complete daily snapshots with an explicit information clock."""

from __future__ import annotations

import csv
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .contracts import AgentError, Bar, D, aware, dec, digest, session_time, timestamp

CSV_FIELDS = ["session", "symbol", "open", "high", "low", "close", "volume", "available_at", "source"]


@dataclass(frozen=True)
class MarketView:
    snapshot_id: str
    as_of: datetime
    expected_session: date
    histories: dict[str, tuple[Bar, ...]]


class Snapshot:
    def __init__(self, bars: list[Bar], require_complete: bool = True) -> None:
        if not bars:
            raise AgentError("Dataset is empty")
        self.bars = tuple(sorted(bars, key=lambda b: (b.session, b.symbol)))
        self.symbols = tuple(sorted({b.symbol for b in bars}))
        self.sessions = tuple(sorted({b.session for b in bars}))
        self._rows = {(b.session, b.symbol): b for b in bars}
        if len(self._rows) != len(bars):
            raise AgentError("Duplicate session/symbol rows are not allowed")
        if require_complete and len(self.sessions) * len(self.symbols) != len(bars):
            raise AgentError("Incomplete universe/session grid; resolve missing bars explicitly")
        self._by_symbol = {symbol: tuple(b for b in self.bars if b.symbol == symbol) for symbol in self.symbols}
        self.id = digest(self.bars)

    def day(self, day: date) -> dict[str, Bar]:
        return {symbol: self._rows[(day, symbol)] for symbol in self.symbols if (day, symbol) in self._rows}

    def view(self, as_of: datetime, expected_session: date, allow_missing: bool = False) -> MarketView:
        at = aware(as_of)
        histories = {
            symbol: tuple(b for b in self._by_symbol[symbol] if b.session <= expected_session
                          and aware(b.available_at) <= at)
            for symbol in self.symbols
        }
        if not allow_missing and any(not h or h[-1].session != expected_session for h in histories.values()):
            raise AgentError("Expected final bars are unavailable or stale at decision time")
        return MarketView(self.id, at, expected_session, histories)


def read_csv(path: str | Path, require_complete: bool = True) -> Snapshot:
    bars = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not set(CSV_FIELDS[:-1]) <= set(reader.fieldnames or ()):
            raise AgentError(f"CSV requires columns: {', '.join(CSV_FIELDS[:-1])}")
        for number, row in enumerate(reader, 2):
            try:
                if row.get("adjusted_close") and dec(row["adjusted_close"]) != dec(row["close"]):
                    raise AgentError("Corporate-action accounting is not implemented; adjusted/raw mismatch")
                bars.append(Bar(date.fromisoformat(row["session"]), row["symbol"],
                                *(dec(row[k]) for k in ("open", "high", "low", "close")),
                                int(row["volume"]), timestamp(row["available_at"]),
                                row.get("source") or "user-supplied"))
            except (ValueError, KeyError, ArithmeticError) as exc:
                raise AgentError(f"CSV line {number}: {exc}") from exc
    return Snapshot(bars, require_complete=require_complete)


def write_csv(snapshot: Snapshot, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for b in snapshot.bars:
            writer.writerow({k: getattr(b, k) for k in CSV_FIELDS})


def synthetic_snapshot(sessions: int = 220, seed: int = 7) -> Snapshot:
    """Synthetic weekdays, not an exchange calendar or evidence of alpha."""
    if sessions < 60:
        raise AgentError("Synthetic demo requires at least 60 sessions")
    rng = random.Random(seed)
    day = date(2024, 1, 2)
    symbols = ("DEMO_A", "DEMO_B", "DEMO_C")
    last = {s: D(10000 + i * 2500) for i, s in enumerate(symbols)}
    bars = []
    i = 0
    while i < sessions:
        if day.weekday() < 5:
            for j, symbol in enumerate(symbols):
                opening = (last[symbol] * dec(1 + rng.gauss(0, .002))).quantize(D("0.01"))
                change = .00025 + .003 * math.sin(i / 18 + j * 2) + rng.gauss(0, .006)
                closing = (opening * dec(1 + change)).quantize(D("0.01"))
                bars.append(Bar(day, symbol, opening, max(opening, closing) * D("1.003"),
                                min(opening, closing) * D("0.997"), closing,
                                rng.randint(50000, 200000), session_time(day, 15, 40), "SYNTHETIC"))
                last[symbol] = closing
            i += 1
        day += timedelta(days=1)
    return Snapshot(bars)
