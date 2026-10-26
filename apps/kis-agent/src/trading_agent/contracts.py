"""Shared validated contracts. Currency calculations use Decimal throughout."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

D = Decimal
KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc


class AgentError(ValueError):
    """An actionable data, configuration, risk, or state error."""


def dec(value: Any) -> Decimal:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean is not a number")
        result = D(str(value))
        if not result.is_finite():
            raise ValueError("nonfinite")
        return result
    except (ValueError, ArithmeticError) as exc:
        raise AgentError(f"Invalid finite decimal: {value!r}") from exc


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AgentError("Timezone-aware timestamps are required")
    return value.astimezone(UTC)


def timestamp(text: str) -> datetime:
    return aware(datetime.fromisoformat(text.replace("Z", "+00:00")))


def session_time(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), KST).astimezone(UTC)


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(value, default=json_default, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def kis_account(text: str) -> str:
    """The canonical 8 digits-01 account, accepting the hyphen-free 10-digit form; empty if invalid."""
    match = re.fullmatch(r"([0-9]{8})-?(01)", text)
    return f"{match[1]}-{match[2]}" if match else ""


@lru_cache(maxsize=1)
def implementation_id() -> str:
    """Bind artifacts to actual packaged source, not just a manually bumped label."""
    files = sorted(Path(__file__).parent.glob("*.py"))
    return digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files})


@dataclass(frozen=True)
class Bar:
    session: date
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    available_at: datetime
    source: str = "user-supplied"

    def __post_init__(self) -> None:
        object.__setattr__(self, "available_at", aware(self.available_at))
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if not self.symbol or not self.symbol.isascii() or not self.symbol.replace("_", "").isalnum():
            raise AgentError("Symbol must contain ASCII letters, digits or underscores")
        if any(not dec(p).is_finite() or p <= 0 for p in (self.open, self.high, self.low, self.close)):
            raise AgentError("Prices must be positive finite decimals")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise AgentError("OHLC prices are inconsistent")
        if not isinstance(self.volume, int) or isinstance(self.volume, bool) or self.volume < 0:
            raise AgentError("Volume must be a nonnegative integer")
        if aware(self.available_at) < session_time(self.session, 15, 30):
            raise AgentError("A final KRX daily bar cannot be available before its close")


@dataclass(frozen=True)
class StrategySpec:
    lookback: int = 10
    top_n: int = 2
    gross_weight: Decimal = D("0.8")
    min_momentum: Decimal = D("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "gross_weight", dec(self.gross_weight))
        object.__setattr__(self, "min_momentum", dec(self.min_momentum))
        if type(self.lookback) is not int or self.lookback < 2:
            raise AgentError("lookback must be an integer >= 2")
        if type(self.top_n) is not int or self.top_n < 1:
            raise AgentError("top_n must be a positive integer")
        if not D(0) < self.gross_weight <= D(1):
            raise AgentError("gross_weight must be in (0, 1]")
        dec(self.min_momentum)

    @property
    def version(self) -> str:
        return digest({"implementation": implementation_id(), "strategy": "momentum-v1", "parameters": self})


@dataclass(frozen=True)
class Costs:
    """Example rates, not a statement of actual KIS account/product fees."""

    commission_bps: Decimal = D("1.5")
    sell_tax_bps: Decimal = D("0")
    slippage_bps: Decimal = D("5")

    def __post_init__(self) -> None:
        for name in ("commission_bps", "sell_tax_bps", "slippage_bps"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        if any(not D(0) <= dec(x) < D(10000) for x in (
            self.commission_bps, self.sell_tax_bps, self.slippage_bps
        )):
            raise AgentError("Costs must be finite nonnegative basis points below 10000")

    def fee(self, side: str, quantity: int, price: Decimal) -> Decimal:
        rate = self.commission_bps + (self.sell_tax_bps if side == "SELL" else D(0))
        return (quantity * price * rate / D(10000)).quantize(D("0.01"), ROUND_HALF_UP)

    def reserve_fee(self, side: str, quantity: int, price: Decimal) -> Decimal:
        """Reserve the worst rounding case if every share fills separately."""
        rate = self.commission_bps + (self.sell_tax_bps if side == "SELL" else D(0))
        per_share = (price * rate / D(10000)).quantize(D("0.01"), ROUND_CEILING)
        return per_share * quantity

    def execution_price(self, side: str, price: Decimal) -> Decimal:
        direction = D(1) if side == "BUY" else D(-1)
        return (price * (D(1) + direction * self.slippage_bps / D(10000))).quantize(
            D("0.01"), ROUND_HALF_UP)


@dataclass
class Wallet:
    cash: Decimal
    positions: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = dec(self.cash)
        if self.cash < 0 or any(type(q) is not int or q < 0 for q in self.positions.values()):
            raise AgentError("Invalid long-only cash account")

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        if any(q and symbol not in marks for symbol, q in self.positions.items()):
            raise AgentError("Missing position price; cannot value portfolio")
        return self.cash + sum((q * marks[s] for s, q in self.positions.items() if q), D(0))

    def fill(self, symbol: str, side: str, quantity: int, price: Decimal, fee: Decimal,
             notional: Decimal | None = None) -> None:
        price, fee = dec(price), dec(fee)
        if type(quantity) is not int or quantity <= 0 or price <= 0 or fee < 0:
            raise AgentError("Invalid fill")
        held = self.positions.get(symbol, 0)
        value = quantity * price if notional is None else dec(notional)
        if value <= 0:
            raise AgentError("Fill notional must be positive")
        if side == "BUY":
            amount = value + fee
            if amount > self.cash:
                raise AgentError("Fill exceeds cash; borrowing is disabled")
            self.cash -= amount
            self.positions[symbol] = held + quantity
        elif side == "SELL":
            if quantity > held:
                raise AgentError("Fill exceeds holdings; shorting is disabled")
            self.cash += value - fee
            self.positions[symbol] = held - quantity
        else:
            raise AgentError("side must be BUY or SELL")


@dataclass(frozen=True)
class Decision:
    strategy_version: str
    snapshot_id: str
    as_of: datetime
    valid_until: datetime
    target_weights: dict[str, Decimal]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", aware(self.as_of))
        object.__setattr__(self, "valid_until", aware(self.valid_until))
        object.__setattr__(self, "target_weights", {s: dec(w) for s, w in self.target_weights.items()})
        if aware(self.valid_until) <= aware(self.as_of):
            raise AgentError("Decision expiry must follow its creation")
        if any(not D(0) <= dec(w) <= D(1) for w in self.target_weights.values()):
            raise AgentError("Only long-only unleveraged targets are supported")
        if sum(self.target_weights.values(), D(0)) > D(1):
            raise AgentError("Target weights exceed 100%")


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", aware(self.as_of))
        object.__setattr__(self, "price", dec(self.price))
        if dec(self.price) <= 0:
            raise AgentError("Quote price must be positive")


@dataclass(frozen=True)
class OrderIntent:
    key: str
    account: str
    symbol: str
    side: str
    quantity: int
    limit_price: Decimal
    created_at: datetime
    expires_at: datetime
    strategy_version: str
    snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", aware(self.created_at))
        object.__setattr__(self, "expires_at", aware(self.expires_at))
        object.__setattr__(self, "limit_price", dec(self.limit_price))
        if self.side not in {"BUY", "SELL"} or type(self.quantity) is not int or self.quantity <= 0:
            raise AgentError("Order requires BUY/SELL and positive integer quantity")
        if dec(self.limit_price) <= 0 or aware(self.expires_at) <= aware(self.created_at):
            raise AgentError("Invalid order price or expiry")
        if not all((self.key, self.account, self.strategy_version, self.snapshot_id)):
            raise AgentError("Order provenance is required")
