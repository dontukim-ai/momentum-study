"""Price snapshot indexed for research-scale histories (LOOP-05c).

``trading_agent.data.Snapshot`` groups bars by scanning the whole history once
per symbol, re-hashes every bar on construction and filters each symbol's full
history on every daily view. On the KRX supplier history (about 2,700 symbols
over 1,100 sessions) every ``ResearchInput.prefix`` and fingerprint paid that
again, and the observation ledger after an exhausted validation budget did not
finish in hours (explore v2c monthly, 2026-10-22).

``IndexedSnapshot`` holds exactly the same bars, ids, days and views. It groups
bars in one pass, answers views by bisection, and derives prefixes by slicing.
Prefix ids come from one streaming SHA-256 pass: ``canonical(bars)`` is
``"[" + ",".join(canonical(bar)) + "]"``, so the hash state after the first
``n`` bars extends to every longer prefix. The shared kis-agent package is left
unchanged because its source hash is part of every strategy version.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from trading_agent.contracts import AgentError, Bar, aware, canonical
from trading_agent.data import MarketView, Snapshot


class _PrefixDigest:
    """``digest(bars[:n])`` for any session-boundary ``n`` of one sorted history."""

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self.bars = bars
        self._hash = hashlib.sha256(b"[")
        self._done = 0
        # Hash states at every session boundary already streamed, so an
        # earlier prefix requested late does not restart the stream.
        self._states: dict[int, Any] = {}
        self._ids: dict[int, str] = {}
        self._precision: dict[Decimal, int] = {}

    def id(self, length: int) -> str:
        if length in self._ids:
            return self._ids[length]
        if length > self._done:
            bars = self.bars
            for index in range(self._done, length):
                self._hash.update(
                    (b"," if index else b"") + canonical(bars[index]).encode()
                )
                following = index + 1
                if following < len(bars) and bars[following].session != bars[index].session:
                    self._states[following] = self._hash.copy()
            self._done = length
            state = self._hash.copy()
        elif length == self._done:
            state = self._hash.copy()
        elif length in self._states:
            state = self._states[length].copy()
        else:
            raise AgentError("Snapshot prefixes end at session boundaries")
        state.update(b"]")
        self._ids[length] = state.hexdigest()
        return self._ids[length]

    def first_fractional(self, quantum: Decimal) -> int:
        """Index of the first bar whose prices are finer than ``quantum``."""
        if quantum not in self._precision:
            self._precision[quantum] = next(
                (
                    index
                    for index, b in enumerate(self.bars)
                    if any(
                        v != v.quantize(quantum) for v in (b.open, b.close, b.high, b.low)
                    )
                ),
                len(self.bars),
            )
        return self._precision[quantum]


class IndexedSnapshot(Snapshot):
    def __init__(self, bars: Iterable[Bar], require_complete: bool = True) -> None:
        # Same validation, order and messages as Snapshot.__init__.
        items = list(bars)
        if not items:
            raise AgentError("Dataset is empty")
        ordered = tuple(sorted(items, key=lambda b: (b.session, b.symbol)))
        self._build(ordered, _PrefixDigest(ordered), len(ordered))
        if len(self._rows) != len(ordered):
            raise AgentError("Duplicate session/symbol rows are not allowed")
        if require_complete and len(self.sessions) * len(self.symbols) != len(ordered):
            raise AgentError(
                "Incomplete universe/session grid; resolve missing bars explicitly"
            )

    @classmethod
    def of(cls, snapshot: Snapshot) -> IndexedSnapshot:
        """Index an already validated snapshot, keeping its computed id."""
        if isinstance(snapshot, IndexedSnapshot):
            return snapshot
        indexed = cls.__new__(cls)
        prefix = _PrefixDigest(snapshot.bars)
        prefix._ids[len(snapshot.bars)] = snapshot.id
        indexed._build(snapshot.bars, prefix, len(snapshot.bars))
        return indexed

    def _build(self, bars: tuple[Bar, ...], prefix: _PrefixDigest, length: int) -> None:
        self.bars = bars
        self._digest = prefix
        self._length = length
        rows: dict[tuple[date, str], Bar] = {}
        grouped: dict[str, list[Bar]] = {}
        sessions: list[date] = []
        for b in bars:
            rows[(b.session, b.symbol)] = b
            grouped.setdefault(b.symbol, []).append(b)
            if not sessions or sessions[-1] != b.session:
                sessions.append(b.session)
        self._rows = rows
        self.symbols = tuple(sorted(grouped))
        self.sessions = tuple(sessions)
        self._by_symbol = {s: tuple(grouped[s]) for s in self.symbols}
        self._bar_sessions = [b.session for b in bars]
        self._symbol_sessions = {
            s: [b.session for b in history] for s, history in self._by_symbol.items()
        }
        self._published: dict[str, list[datetime]] = {}
        for s, history in self._by_symbol.items():
            latest: list[datetime] = []
            for b in history:
                at = aware(b.available_at)
                latest.append(at if not latest or at > latest[-1] else latest[-1])
            self._published[s] = latest

    @property  # type: ignore[override]
    def id(self) -> str:
        return self._digest.id(self._length)

    def whole_prices(self, quantum: Decimal) -> bool:
        return self._digest.first_fractional(quantum) >= self._length

    def day(self, day: date) -> dict[str, Bar]:
        if day > self.sessions[-1]:
            return {}
        return {
            symbol: self._rows[(day, symbol)]
            for symbol in self.symbols
            if (day, symbol) in self._rows
        }

    def view(
        self, as_of: datetime, expected_session: date, allow_missing: bool = False
    ) -> MarketView:
        at = aware(as_of)
        histories: dict[str, tuple[Bar, ...]] = {}
        for symbol in self.symbols:
            count = bisect.bisect_right(self._symbol_sessions[symbol], expected_session)
            history = self._by_symbol[symbol]
            if count and self._published[symbol][count - 1] <= at:
                histories[symbol] = history[:count]
            else:
                histories[symbol] = tuple(
                    b for b in history[:count] if aware(b.available_at) <= at
                )
        if not allow_missing and any(
            not h or h[-1].session != expected_session for h in histories.values()
        ):
            raise AgentError("Expected final bars are unavailable or stale at decision time")
        return MarketView(self.id, at, expected_session, histories)

    def until(self, end: date) -> IndexedSnapshot:
        """Bars with ``session <= end``, as ``Snapshot(..., require_complete=False)``."""
        cut = bisect.bisect_right(self._bar_sessions, end, 0, self._length)
        if not cut:
            raise AgentError("Dataset is empty")
        if cut == self._length:
            return self
        child = IndexedSnapshot.__new__(IndexedSnapshot)
        child.bars = self.bars[:cut]
        child._digest = self._digest
        child._length = cut
        # Rows stay shared: day() never answers past the child's last session,
        # and every row up to it belongs to the child.
        child._rows = self._rows
        child.sessions = self.sessions[: bisect.bisect_right(self.sessions, end)]
        child._bar_sessions = self._bar_sessions
        child._by_symbol, child._symbol_sessions, child._published = {}, {}, {}
        for symbol in self.symbols:
            count = bisect.bisect_right(self._symbol_sessions[symbol], end)
            if count:
                child._by_symbol[symbol] = self._by_symbol[symbol][:count]
                child._symbol_sessions[symbol] = self._symbol_sessions[symbol][:count]
                child._published[symbol] = self._published[symbol][:count]
        child.symbols = tuple(child._by_symbol)
        return child

