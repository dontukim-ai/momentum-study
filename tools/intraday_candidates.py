"""The 27 intraday candidates of intraday-dev-plan-v11 §3, as target-weight strategies.

Each class only returns target weights for tools/intraday_engine.py; the
engine fills, charges and books them. Choices the plan left open (fixed here
before any development bar is read, intraday-dev-plan-v11 §7):

- weights: one-ETF candidates (C1, C5) use 0.5, the engine's per-ETF cap;
  C2/C3 hold k ETFs at 1/k; C4 takes 0.2 a position, at most 5 at once; C5-A's
  four-ETF basket holds 0.125 each so its exposure matches the one-ETF runs.
- an entry "at 15:00" is decided at the end of 14:59 and fills at 15:00's open.
- C5-B cannot see today's open before the open auction, so it reads the gap
  from the 09:00 bar's open and buys at the next minute (continuous, one tick).
- "large 8" (C4) are the eight market-index ETFs of the fixed list; the
  "four index ETFs" (C5-A) are the cost-probe ETFs 069500, 102110, 229200, 278530.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal as D
from itertools import pairwise
from typing import Any

KOSPI, KOSPI_INV, KOSDAQ, KOSDAQ_INV = "069500", "114800", "229200", "251340"
LARGE8 = ("069500", "102110", "148020", "105190", "278530", "226490", "229200", "232080")
INDEX4 = ("069500", "102110", "229200", "278530")
SLOT_STARTS = ("090000", "093000", "100000", "103000", "110000", "113000", "120000", "123000",
               "130000", "133000", "140000", "143000", "150000")
END = "152000"


def minutes(hhmmss: str) -> int:
    return int(hhmmss[:2]) * 60 + int(hhmmss[2:4])


def hhmm(total: int) -> str:
    return f"{total // 60:02d}{total % 60:02d}00"


def last_close(bars: Any, before: str) -> D | None:
    """Close of the last bar strictly before ``before`` (bars are sorted by time)."""
    for bar in reversed(bars):
        if bar.time < before:
            return bar.close
    return None


def previous_close(view: Any, symbol: str) -> D | None:
    for _, symbols in reversed(view.previous_days()):
        bars = symbols.get(symbol)
        if bars:
            return bars[-1].close
    return None


class Base:
    def open_auction(self, view: Any) -> Mapping[str, float] | None:
        return None

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        return None

    def close_auction(self, view: Any) -> Mapping[str, float] | None:
        return None  # flat by default


class IntradayMomentum(Base):
    """C1: sign of prev close -> 09:30 decides index vs inverse, held from ``entry`` to the close auction."""

    def __init__(self, index: str, inverse: str, entry: str, volatile_only: bool):
        self.index, self.inverse, self.volatile_only = index, inverse, volatile_only
        self.decide_at = hhmm(minutes(entry) - 1)
        self.done: str | None = None

    def _morning_vol(self, bars: Any) -> float | None:
        closes = [float(b.close) for b in bars if b.time <= "093000"]
        returns = [b / a - 1 for a, b in pairwise(closes)]
        return statistics.pstdev(returns) if len(returns) >= 10 else None

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        if view.time < self.decide_at or self.done == view.day:
            return None
        self.done = view.day
        base, now = previous_close(view, self.index), last_close(view.bars(self.index), "093100")
        if base is None or now is None or now == base:
            return None
        if self.volatile_only:
            today = self._morning_vol(view.bars(self.index))
            past = [v for _, symbols in view.previous_days()[-20:]
                    if (v := self._morning_vol(symbols.get(self.index, ()))) is not None]
            if today is None or len(past) < 20 or today <= statistics.median(past):
                return None
        return {self.index if now > base else self.inverse: 0.5}


class Periodicity(Base):
    """C2: in each 30-minute slot hold the k ETFs with the best mean return in that slot over the last n days."""

    def __init__(self, symbols: tuple[str, ...], n: int, k: int):
        self.symbols, self.n, self.k = symbols, n, k

    def _slot_return(self, bars: Any, start: str, end: str, before_open: D | None) -> float | None:
        a = before_open if start == SLOT_STARTS[0] else last_close(bars, start)
        b = last_close(bars, end)
        return float(b / a - 1) if a and b else None

    def _targets(self, view: Any, slot: int) -> Mapping[str, float]:
        start = SLOT_STARTS[slot]
        end = SLOT_STARTS[slot + 1] if slot + 1 < len(SLOT_STARTS) else END
        days = view.previous_days()
        if len(days) < self.n:
            return {}
        scores = {}
        for symbol in self.symbols:
            values = []
            for i in range(len(days) - self.n, len(days)):
                # Only the 09:00 slot needs the day before (its start is that day's close).
                prior = days[i - 1][1].get(symbol) if i > 0 else None
                r = self._slot_return(days[i][1].get(symbol, ()), start, end, prior[-1].close if prior else None)
                if r is not None:
                    values.append(r)
            if len(values) == self.n:
                scores[symbol] = statistics.fmean(values)
        best = sorted(scores, key=lambda s: (-scores[s], s))[:self.k]
        return {s: min(1 / self.k, 0.5) for s in best} if len(best) == self.k else {}

    def open_auction(self, view: Any) -> Mapping[str, float] | None:
        return self._targets(view, 0)

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        nxt = hhmm(minutes(view.time) + 1)
        if nxt in SLOT_STARTS[1:]:
            return self._targets(view, SLOT_STARTS.index(nxt))
        return None


class Reversal(Base):
    """C3: every ``hold`` minutes from 09:30 hold the k ETFs with the worst return over the last 30 minutes."""

    def __init__(self, symbols: tuple[str, ...], k: int, hold: int):
        self.symbols, self.k, self.hold = symbols, k, hold

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        nxt = minutes(view.time) + 1
        if nxt < minutes("093000") or nxt >= minutes(END) or (nxt - minutes("093000")) % self.hold:
            return None
        start, end = hhmm(nxt - 30), hhmm(nxt)
        returns = {}
        for symbol in self.symbols:
            bars = view.bars(symbol)
            # The first window starts at the open: its base is the day's first open.
            a = last_close(bars, start) or (bars[0].open if bars and bars[0].time >= start else None)
            b = last_close(bars, end)
            if a and b and any(start <= bar.time < end for bar in bars):
                returns[symbol] = float(b / a - 1)
        worst = sorted(returns, key=lambda s: (returns[s], s))[:self.k]
        return {s: min(1 / self.k, 0.5) for s in worst} if len(worst) == self.k else {}


class VwapDip(Base):
    """C4: buy a dip of z sigma below today's VWAP on a 2x volume minute; exit at VWAP or after 30 minutes."""

    def __init__(self, symbols: tuple[str, ...], z: float, weight: float = 0.2, slots: int = 5):
        self.symbols, self.z, self.weight, self.slots = symbols, z, weight, slots
        self.held: dict[str, int] = {}
        self.day: str | None = None

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        if self.day != view.day:
            self.day, self.held = view.day, {}
        now = minutes(view.time)
        changed = False
        for symbol in list(self.held):
            bars = view.bars(symbol)
            if now - self.held[symbol] >= 30 or (bars and bars[-1].close >= _vwap(bars)):
                del self.held[symbol]
                changed = True
        if view.time >= "093000" and view.time < "150000":
            for symbol in self.symbols:
                if symbol in self.held or len(self.held) >= self.slots:
                    continue
                bars = view.bars(symbol)
                if len(bars) < 61 or bars[-1].time != view.time:
                    continue
                closes = [float(b.close) for b in bars[-61:]]
                sigma = statistics.pstdev([b / a - 1 for a, b in pairwise(closes)])
                volumes = [b.volume for b in bars[-21:-1]]
                gap = float(bars[-1].close / _vwap(bars) - 1)
                if sigma > 0 and gap < -self.z * sigma and bars[-1].volume >= 2 * statistics.fmean(volumes):
                    self.held[symbol] = now
                    changed = True
        return {s: self.weight for s in self.held} if changed else None


def _vwap(bars: Any) -> D:
    volume = sum(b.volume for b in bars)
    return sum((b.close * b.volume for b in bars), D(0)) / volume if volume else bars[-1].close


class Overnight(Base):
    """C5-A: buy at the close auction, sell at the next open auction."""

    def __init__(self, symbols: tuple[str, ...]):
        self.weights = {s: 0.5 / len(symbols) for s in symbols}

    def open_auction(self, view: Any) -> Mapping[str, float] | None:
        return {}

    def close_auction(self, view: Any) -> Mapping[str, float] | None:
        return self.weights


class GapReversal(Base):
    """C5-B: after a gap down of theta at the 09:00 open, buy the next minute and sell at the close auction."""

    def __init__(self, symbol: str, theta: float):
        self.symbol, self.theta = symbol, theta

    def on_minute(self, view: Any) -> Mapping[str, float] | None:
        bars = view.bars(self.symbol)
        if view.time != (bars[0].time if bars else None):
            return None
        base = previous_close(view, self.symbol)
        if base is None or float(bars[0].open / base - 1) > -self.theta:
            return None
        return {self.symbol: 0.5}


@dataclass(frozen=True)
class Config:
    candidate: str
    name: str
    make: Callable[[], Base]


def configs(universe: tuple[str, ...]) -> list[Config]:
    """The 27 fixed combinations (intraday-dev-plan-v11 §3)."""
    out = []
    for market, (index, inverse) in (("kospi", (KOSPI, KOSPI_INV)), ("kosdaq", (KOSDAQ, KOSDAQ_INV))):
        for entry in ("145000", "150000"):
            for volatile in (False, True):
                out.append(Config("C1", f"C1 {market} {entry[:4]} {'vol' if volatile else 'all'}",
                                  lambda i=index, v=inverse, e=entry, w=volatile: IntradayMomentum(i, v, e, w)))
    for n in (5, 20):
        for k in (3, 5):
            out.append(Config("C2", f"C2 n{n} k{k}", lambda n=n, k=k: Periodicity(universe, n, k)))
    for k in (3, 5):
        for hold in (30, 60):
            out.append(Config("C3", f"C3 k{k} hold{hold}", lambda k=k, h=hold: Reversal(universe, k, h)))
    for z in (2.0, 3.0):
        for name, symbols in (("large8", LARGE8), ("all30", universe)):
            out.append(Config("C4", f"C4 z{z:g} {name}", lambda z=z, s=symbols: VwapDip(s, z)))
    for name, symbols in (("069500", (KOSPI,)), ("229200", (KOSDAQ,)), ("index4", INDEX4)):
        out.append(Config("C5", f"C5-A {name}", lambda s=symbols: Overnight(s)))
    for theta in (0.005, 0.01):
        for symbol in (KOSPI, KOSDAQ):
            out.append(Config("C5", f"C5-B {symbol} gap{theta:.1%}", lambda s=symbol, t=theta: GapReversal(s, t)))
    return out
