"""The common intraday engine (intraday-dev-plan-v11 §2). Offline and pure.

Candidates only return target weights; this engine alone fills, charges and
books them (CLAUDE.md rule 7):

- ``on_minute(view)`` runs at the end of each minute t before 15:20 and sees
  bars up to t only. An order fills at the open of the symbol's next bar
  after t and before 15:20 (no bar = no trade, nothing is filled in). A newer
  decision replaces any unfilled one; orders still open at 15:20 expire.
- ``open_auction(view)`` runs before the first bar of the day on earlier days
  only and fills at the first bar's open; ``close_auction(view)`` runs on
  information through 15:19 and fills at the 15:30 bar's close. ``None`` from
  ``close_auction`` means flat: whatever it keeps is held overnight.
- Quantities are floor(weight x equity / last close) at decision time, never
  from the fill price. Each fill pays commission 1.5bp (rounded half up to
  the won) and, in continuous trading only, one adverse tick (5 KRW from
  2,000 KRW, else 1). A fill is capped at 10% of its bar's value (close x
  volume, the closest per-bar value the minute bars carry) and by cash; the
  rest is dropped. Long only, at most 50% per symbol and 100% in total.
- Money is Decimal; cash is reconciled to the won against the trade list.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP
from decimal import Decimal as D
from typing import Any, Protocol

from momentum.config import SpecUndefinedError

CAPITAL = D(100_000_000)
COMMISSION = D("0.00015")
CAPACITY = D("0.1")
MAX_WEIGHT, MAX_GROSS = D("0.5"), D(1)
CONTINUOUS_END, CLOSE_INFO, CLOSE_AUCTION = "152000", "151959", "153000"


@dataclass(frozen=True)
class Bar:
    time: str
    open: D
    high: D
    low: D
    close: D
    volume: int


Days = dict[str, dict[str, tuple[Bar, ...]]]


def days_from_rows(rows: Iterable[Mapping[str, str]]) -> Days:
    """CSV-like rows (day, symbol, time, open, high, low, close, volume) to sorted bars."""
    days: dict[str, dict[str, list[Bar]]] = {}
    for r in rows:
        bar = Bar(r["time"], D(r["open"]), D(r["high"]), D(r["low"]), D(r["close"]), int(r["volume"]))
        days.setdefault(r["day"], {}).setdefault(r["symbol"], []).append(bar)
    return {day: {s: tuple(sorted(b, key=lambda x: x.time)) for s, b in symbols.items()}
            for day, symbols in sorted(days.items())}


def days_from_kis(bars_by_symbol: Mapping[str, Mapping[str, list[dict]]]) -> Days:
    """Saved KIS minute bars ({symbol: {day: [row]}}) to the engine's layout."""
    rows = ({"day": day, "symbol": symbol, "time": r["stck_cntg_hour"], "open": r["stck_oprc"],
             "high": r["stck_hgpr"], "low": r["stck_lwpr"], "close": r["stck_prpr"], "volume": r["cntg_vol"]}
            for symbol, days in bars_by_symbol.items() for day, bars in days.items() for r in bars)
    return days_from_rows(rows)


def tick(price: D) -> D:
    return D(5) if price >= 2000 else D(1)


def won(value: D) -> D:
    return value.quantize(D(1), rounding=ROUND_HALF_UP)


class View:
    """What a strategy may see: earlier days whole, today up to ``time``."""

    def __init__(self, days: Days, day: str, time: str, times: Mapping[str, list[str]] | None = None):
        self.day, self.time, self._days = day, time, days
        # Bar times per symbol for today, so a lookup is a bisection, not a scan.
        self._times = times if times is not None else {
            s: [b.time for b in bars] for s, bars in days[day].items()}

    def bars(self, symbol: str) -> tuple[Bar, ...]:
        today = self._days[self.day].get(symbol, ())
        return today[:bisect_right(self._times.get(symbol, []), self.time)]

    def previous_days(self) -> list[tuple[str, dict[str, tuple[Bar, ...]]]]:
        return [(d, bars) for d, bars in self._days.items() if d < self.day]


class Strategy(Protocol):
    def open_auction(self, view: View) -> Mapping[str, float] | None: ...
    def on_minute(self, view: View) -> Mapping[str, float] | None: ...
    def close_auction(self, view: View) -> Mapping[str, float] | None: ...


@dataclass
class Result:
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    cash: D = CAPITAL
    positions: dict[str, int] = field(default_factory=dict)
    final_equity: D = CAPITAL
    commission_total: D = D(0)
    tick_cost_total: D = D(0)


class _Book:
    def __init__(self, symbols: set[str], capital: D, cost_scale: D = D(1)):
        self.symbols, self.capital, self.scale = symbols, capital, cost_scale
        self.cash = capital
        self.held: dict[str, int] = {}
        self.marks: dict[str, D] = {}
        self.result = Result(cash=capital, final_equity=capital)

    def equity(self) -> D:
        return self.cash + sum((q * self.marks[s] for s, q in self.held.items()), D(0))

    def deltas(self, targets: Mapping[str, float], day: str, time: str) -> dict[str, int]:
        """Validated target weights to share changes, sized at the current marks."""
        weights = {}
        for symbol, raw in targets.items():
            weight = D(str(raw))
            if symbol not in self.symbols or not D(0) <= weight <= MAX_WEIGHT:
                raise SpecUndefinedError(f"target {symbol}={raw}: unknown symbol or weight outside 0..0.5")
            weights[symbol] = weight
        if sum(weights.values(), D(0)) > MAX_GROSS:
            raise SpecUndefinedError(f"targets sum to more than 1 at {day} {time}")
        equity, deltas = self.equity(), {}
        for symbol in sorted(set(weights) | set(self.held)):
            weight = weights.get(symbol, D(0))
            if weight and symbol not in self.marks:
                self.result.events.append({"event": "no_price", "day": day, "time": time, "symbol": symbol})
                continue
            want = int((weight * equity / self.marks[symbol]).to_integral_value(ROUND_FLOOR)) if weight else 0
            if want != self.held.get(symbol, 0):
                deltas[symbol] = want - self.held.get(symbol, 0)
        return deltas

    def fill(self, symbol: str, delta: int, base: D, bar: Bar, kind: str, day: str) -> None:
        side = "BUY" if delta > 0 else "SELL"
        adverse = tick(base) * self.scale if kind == "continuous" else D(0)
        price = base + adverse if delta > 0 else base - adverse
        cap = int((CAPACITY * bar.close * bar.volume / price).to_integral_value(ROUND_FLOOR))
        quantity = min(abs(delta), cap)
        if delta > 0:
            while quantity and quantity * price + won(quantity * price * COMMISSION * self.scale) > self.cash:
                quantity -= 1
        else:
            quantity = min(quantity, self.held.get(symbol, 0))
        if quantity < abs(delta):
            self.result.events.append({"event": "capacity_cut" if quantity == min(abs(delta), cap) else "cash_cut",
                                       "day": day, "time": bar.time, "symbol": symbol, "requested": abs(delta),
                                       "filled": quantity})
        if not quantity:
            return
        notional = quantity * price
        commission = won(notional * COMMISSION * self.scale)
        signed = quantity if delta > 0 else -quantity
        self.cash += -notional - commission if delta > 0 else notional - commission
        held = self.held.get(symbol, 0) + signed
        if held:
            self.held[symbol] = held
        else:
            self.held.pop(symbol, None)
        trade = {"day": day, "time": bar.time, "kind": kind, "symbol": symbol, "side": side, "quantity": quantity,
                 "price": price, "notional": notional, "commission": commission, "tick_cost": adverse * quantity}
        self.result.trades.append(trade)
        self.result.events.append({"event": "fill", **trade})
        self.result.commission_total += commission
        self.result.tick_cost_total += adverse * quantity

    def auction(self, targets: Mapping[str, float], day: str, time: str, bars: Mapping[str, Bar],
                kind: str, price_of: str) -> None:
        deltas = self.deltas(targets, day, time)
        for symbol, delta in sorted(deltas.items(), key=lambda item: item[1] > 0):  # sells first
            bar = bars.get(symbol)
            if bar is None:
                self.result.events.append({"event": "unfilled", "kind": kind, "day": day, "symbol": symbol})
                continue
            self.fill(symbol, delta, getattr(bar, price_of), bar, kind, day)


def run(days: Days, strategy: Strategy, capital: D = CAPITAL, cost_scale: float = 1) -> Result:
    """``cost_scale`` multiplies commission and tick (the plan's twice-the-cost check)."""
    book = _Book({s for symbols in days.values() for s in symbols}, capital, D(str(cost_scale)))
    result = book.result
    for day, symbols in days.items():
        start = book.equity()
        times = sorted({b.time for bars in symbols.values() for b in bars})
        by_time: dict[str, dict[str, Bar]] = {}
        for symbol, bars in symbols.items():
            for b in bars:
                by_time.setdefault(b.time, {})[symbol] = b
        bar_times = {s: [b.time for b in bars] for s, bars in symbols.items()}
        opening = times[0]
        before = _second_before(opening)
        targets = strategy.open_auction(View(days, day, before, bar_times))
        if targets is not None:
            book.auction(targets, day, before, by_time[opening], "open_auction", "open")
        pending: dict[str, int] = {}
        for time in (t for t in times if t < CONTINUOUS_END):
            for symbol, bar in sorted(by_time[time].items()):
                if symbol in pending:
                    book.fill(symbol, pending.pop(symbol), bar.open, bar, "continuous", day)
                book.marks[symbol] = bar.close
            decided = strategy.on_minute(View(days, day, time, bar_times))
            if decided is not None:
                pending = book.deltas(decided, day, time)
        for symbol in sorted(pending):
            result.events.append({"event": "expired", "day": day, "symbol": symbol, "requested": pending[symbol]})
        closing = strategy.close_auction(View(days, day, CLOSE_INFO, bar_times))
        book.auction(closing or {}, day, CLOSE_INFO, by_time.get(CLOSE_AUCTION, {}), "close_auction", "close")
        for time in (t for t in times if t >= CONTINUOUS_END):
            for symbol, bar in by_time[time].items():
                book.marks[symbol] = bar.close
        end = book.equity()
        result.daily.append({"day": day, "start_equity": start, "end_equity": end, "pnl": end - start,
                             "positions": dict(book.held)})
    _reconcile(book, capital)
    result.cash, result.positions, result.final_equity = book.cash, dict(book.held), book.equity()
    return result


def _second_before(hhmmss: str) -> str:
    seconds = int(hhmmss[:2]) * 3600 + int(hhmmss[2:4]) * 60 + int(hhmmss[4:]) - 1
    return f"{seconds // 3600:02d}{seconds % 3600 // 60:02d}{seconds % 60:02d}"


def _reconcile(book: _Book, capital: D) -> None:
    cash = capital
    held: dict[str, int] = {}
    for t in book.result.trades:
        sign = 1 if t["side"] == "BUY" else -1
        cash += -sign * t["notional"] - t["commission"]
        held[t["symbol"]] = held.get(t["symbol"], 0) + sign * t["quantity"]
    if cash != book.cash or {s: q for s, q in held.items() if q} != book.held:
        raise SpecUndefinedError(f"ledger does not reconcile: cash {cash} vs {book.cash}")
