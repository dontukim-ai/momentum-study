"""Point-in-time security screening and explainable, bounded ranking rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .contracts import (
    AgentError,
    Costs,
    D,
    Decision,
    StrategySpec,
    Wallet,
    aware,
    canonical,
    dec,
    digest,
    implementation_id,
    timestamp,
)
from .data import MarketView
from .execution import RiskPolicy

RANKINGS = ("momentum", "risk_adjusted", "trend_stability")


@dataclass(frozen=True)
class RankedStrategy(StrategySpec):
    ranking: str = "momentum"

    def __post_init__(self):
        super().__post_init__()
        if self.ranking not in RANKINGS or self.lookback > 60:
            raise AgentError("Ranking must be allowlisted and lookback must be 2..60")

    @property
    def version(self):
        return digest({"implementation": implementation_id(), "strategy": "screened-ranking-v1", "parameters": self})


def ranked_spec(raw: dict) -> RankedStrategy:
    try:
        return RankedStrategy(**raw)
    except (TypeError, ValueError):
        raise AgentError("Invalid bounded ranking strategy") from None


@dataclass(frozen=True)
class SecurityState:
    symbol: str
    effective_at: datetime
    available_at: datetime
    status: str
    asset_type: str
    market: str
    managed: bool
    caution: bool
    leveraged: bool
    inverse: bool
    bid: D | None
    ask: D | None
    source: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,30}", self.symbol):
            raise AgentError("Invalid security symbol")
        for name in ("effective_at", "available_at"):
            object.__setattr__(self, name, aware(getattr(self, name)))
        if self.status not in {"NORMAL", "SUSPENDED", "DELISTED"} or self.asset_type not in {"STOCK", "ETF", "ETN", "OTHER"}:
            raise AgentError("Unknown security status or asset type")
        if self.market not in {"KOSPI", "KOSDAQ", "KONEX"} or not isinstance(self.source, str) or not self.source.strip():
            raise AgentError("Explicit market and source are required")
        if any(type(getattr(self, name)) is not bool for name in ("managed", "caution", "leveraged", "inverse")):
            raise AgentError("Security flags must be explicit booleans")
        if (self.bid is None) != (self.ask is None):
            raise AgentError("Bid and ask must both be present or both unknown")
        if self.bid is not None:
            bid, ask = dec(self.bid), dec(self.ask)
            if not D(0) < bid <= ask:
                raise AgentError("Invalid security bid/ask")
            object.__setattr__(self, "bid", bid)
            object.__setattr__(self, "ask", ask)


class SecurityHistory:
    def __init__(self, records: list[SecurityState]):
        if not records:
            raise AgentError("Security history is empty")
        self.records = tuple(sorted(records, key=lambda r: (r.symbol, r.effective_at, r.available_at)))
        keys = {(r.symbol, r.effective_at, r.available_at) for r in records}
        if len(keys) != len(records):
            raise AgentError("Duplicate security history timestamp")
        self.id = digest(self.records)
        self._by_symbol = {}
        self._state_cache = {}
        for record in self.records:
            self._by_symbol.setdefault(record.symbol, []).append(record)

    def visible(self, at: datetime) -> tuple[SecurityState, ...]:
        at = aware(at)
        return tuple(r for r in self.records if r.effective_at <= at and r.available_at <= at)

    def states(self, at: datetime) -> dict[str, SecurityState]:
        # Effective time takes precedence; late revisions to older events cannot
        # replace a newer state. Only versions actually published by at are used.
        at = aware(at)
        if at in self._state_cache:
            return dict(self._state_cache[at])
        result = {}
        for symbol, records in self._by_symbol.items():
            visible = [r for r in records if r.effective_at <= at and r.available_at <= at]
            if visible:
                result[symbol] = max(visible, key=lambda r: (r.effective_at, r.available_at))
        self._state_cache[at] = result
        return dict(result)


def read_security_history(path: Path) -> SecurityHistory:
    records = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                raw["effective_at"], raw["available_at"] = timestamp(raw["effective_at"]), timestamp(raw["available_at"])
                records.append(SecurityState(**raw))
            except (KeyError, TypeError, ValueError, AttributeError):
                raise AgentError(f"Invalid security JSONL record at line {number}") from None
    return SecurityHistory(records)


def write_security_history(history: SecurityHistory, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical(record) + "\n" for record in history.records), encoding="utf-8")


@dataclass(frozen=True)
class SelectionPolicy:
    capital_limit: D = D("1000000")
    min_price: D = D("1000")
    min_average_turnover: D = D("100000000")
    min_average_volume: D = D("10000")
    max_daily_volatility: D = D(".08")
    max_spread_bps: D = D("30")
    max_state_age_seconds: int = 86400
    min_history: int = 60
    feature_window: int = 20
    allowed_types: tuple[str, ...] = ("STOCK", "ETF")
    allowed_markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    max_gross: D = D(".8")
    max_position: D = D(".4")
    max_order_notional: D = D("400000")
    spread_mode: str = "quoted"

    def __post_init__(self):
        if self.spread_mode not in ("quoted", "historical_unavailable"):
            raise AgentError("Unsupported spread mode")
        for name in ("capital_limit", "min_price", "min_average_turnover", "min_average_volume", "max_daily_volatility",
                     "max_spread_bps", "max_gross", "max_position", "max_order_notional"):
            object.__setattr__(self, name, dec(getattr(self, name)))
        for name in ("allowed_types", "allowed_markets"):
            value = tuple(getattr(self, name))
            if not value or len(set(value)) != len(value):
                raise AgentError("Explicit unique selection types and markets are required")
            object.__setattr__(self, name, value)
        if set(self.allowed_types) - {"STOCK", "ETF"} or set(self.allowed_markets) - {"KOSPI", "KOSDAQ", "KONEX"}:
            raise AgentError("Unsupported selection universe")
        if not D(0) < self.capital_limit <= D("1000000") or self.min_price <= 0:
            raise AgentError("Research capital must be positive and at most the requested 1000000 KRW")
        if any(getattr(self, n) < 0 for n in ("min_average_turnover", "min_average_volume")):
            raise AgentError("Negative liquidity thresholds are invalid")
        if not D(0) < self.max_daily_volatility <= D(1) or self.max_spread_bps <= 0:
            raise AgentError("Volatility and spread limits must be positive")
        if any(type(getattr(self, n)) is not int for n in ("max_state_age_seconds", "min_history", "feature_window")):
            raise AgentError("History and age limits must be integers")
        if self.max_state_age_seconds <= 0 or not 2 <= self.feature_window < self.min_history:
            raise AgentError("Invalid history or metadata age policy")
        self.risk(("VALIDATION",))

    def risk(self, symbols: tuple[str, ...]):
        return RiskPolicy(symbols, self.max_gross, self.max_position, self.max_order_notional,
                          version="research-selection-fixed-risk-v1")


def select_candidates(spec: RankedStrategy, view: MarketView, securities: SecurityHistory,
                      policy: SelectionPolicy, costs: Costs, valid_until: datetime,
                      equity: D | None = None, *, audit: bool = True) -> dict:
    if spec.gross_weight > policy.max_gross or spec.gross_weight / spec.top_n > policy.max_position:
        raise AgentError("Strategy allocation exceeds the fixed selection risk policy")
    full_equity = policy.capital_limit if equity is None else dec(equity)
    if full_equity <= 0:
        raise AgentError("Cannot screen a nonpositive portfolio")
    capital = min(full_equity, policy.capital_limit)
    allocation = min(capital * spec.gross_weight / spec.top_n, capital * policy.max_position, policy.max_order_notional)
    states = securities.states(view.as_of)
    visible_histories = {symbol: h for symbol, h in view.histories.items() if h}
    required_history = max(policy.min_history, spec.lookback + 1)
    expected_sessions = tuple(sorted({b.session for h in visible_histories.values() for b in h}))[-required_history:]
    symbols = sorted(set(states) | set(visible_histories))
    rows = []
    for symbol in symbols:
        history, state, reasons, features = visible_histories.get(symbol, ()), states.get(symbol), [], {}
        if state is None:
            reasons.append("SECURITY_METADATA_UNAVAILABLE")
        else:
            if state.status != "NORMAL":
                reasons.append(state.status)
            if state.managed:
                reasons.append("MANAGED_SECURITY")
            if state.caution:
                reasons.append("CAUTION_SECURITY")
            if state.leveraged or state.inverse:
                reasons.append("LEVERAGED_OR_INVERSE")
            if state.asset_type not in policy.allowed_types or state.market not in policy.allowed_markets:
                reasons.append("OUTSIDE_ALLOWED_UNIVERSE")
            if (view.as_of - state.effective_at).total_seconds() > policy.max_state_age_seconds:
                reasons.append("STALE_SECURITY_STATE")
            if state.bid is None:
                if policy.spread_mode == "historical_unavailable":
                    features["spread_source"] = "unavailable_preregistered"
                else:
                    reasons.append("SPREAD_UNAVAILABLE")
            else:
                if policy.spread_mode == "historical_unavailable":
                    features["spread_source"] = "quoted"
                features["spread_bps"] = (state.ask - state.bid) / ((state.ask + state.bid) / 2) * 10000
                if features["spread_bps"] > policy.max_spread_bps:
                    reasons.append("WIDE_SPREAD")
        if not history or history[-1].session != view.expected_session:
            reasons.append("CURRENT_FINAL_BAR_UNAVAILABLE")
        if len(history) < max(policy.min_history, spec.lookback + 1):
            reasons.append("INSUFFICIENT_HISTORY")
        elif tuple(b.session for b in history[-required_history:]) != expected_sessions:
            reasons.append("MISSING_RECENT_SESSIONS")
        if history:
            last = history[-1]
            features["price"], features["history_count"] = last.close, len(history)
            if (view.as_of - last.available_at).total_seconds() > policy.max_state_age_seconds:
                reasons.append("STALE_FINAL_BAR")
            if last.close < policy.min_price:
                reasons.append("PRICE_BELOW_MINIMUM")
            price = costs.execution_price("BUY", last.close)
            unit_cost = price + costs.reserve_fee("BUY", 1, price)
            features["affordable_quantity"] = max(0, int(allocation / unit_cost))
            if features["affordable_quantity"] < 1:
                reasons.append("UNAFFORDABLE_WITHIN_POSITION_BUDGET")
        if len(history) >= max(policy.min_history, spec.lookback + 1):
            tail = history[-policy.feature_window:]
            returns = [b.close / a.close - 1 for a, b in zip(history[-policy.feature_window-1:], history[-policy.feature_window:])]
            mean = sum(returns, D(0)) / len(returns)
            volatility = (sum(((r - mean) ** 2 for r in returns), D(0)) / len(returns)).sqrt()
            momentum = history[-1].close / history[-spec.lookback-1].close - 1
            features.update(momentum=momentum, daily_volatility=volatility,
                            average_turnover_proxy=sum((b.close * b.volume for b in tail), D(0)) / len(tail),
                            average_volume=D(sum(b.volume for b in tail)) / len(tail),
                            positive_return_fraction=D(sum(r > 0 for r in returns)) / len(returns))
            if features["average_turnover_proxy"] < policy.min_average_turnover or features["average_volume"] < policy.min_average_volume:
                reasons.append("LOW_LIQUIDITY")
            if volatility > policy.max_daily_volatility:
                reasons.append("EXCESSIVE_VOLATILITY")
            if momentum <= spec.min_momentum:
                reasons.append("MOMENTUM_BELOW_THRESHOLD")
            score = (momentum if spec.ranking == "momentum" else momentum / max(volatility, D(".001"))
                     if spec.ranking == "risk_adjusted" else momentum * features["positive_return_fraction"])
            features["score"] = score
        rows.append({"symbol": symbol, "status": "REJECTED" if reasons else "ELIGIBLE", "reasons": reasons,
                     "features": features, "security_source": state.source if state else None})
    eligible = sorted((r for r in rows if r["status"] == "ELIGIBLE"), key=lambda r: (-r["features"]["score"], r["symbol"]))
    for rank, row in enumerate(eligible, 1):
        row["rank"] = rank
        row["status"] = "SELECTED" if rank <= spec.top_n else "NOT_SELECTED"
        if rank > spec.top_n:
            row["reasons"] = ["OUTSIDE_TOP_N"]
    selected = [r["symbol"] for r in eligible[:spec.top_n]]
    visible_id = digest({"bars": visible_histories, "securities": securities.visible(view.as_of)}) if audit else view.snapshot_id
    decision = Decision(spec.version, visible_id, view.as_of, valid_until,
                        {symbol: allocation / full_equity for symbol in selected})
    return {"mode": "RESEARCH_SELECTION_ONLY", "as_of": view.as_of, "session": view.expected_session,
            "visible_input_id": visible_id, "strategy": spec, "strategy_version": spec.version,
            "policy_hash": digest(policy), "capital_limit": policy.capital_limit, "sizing_equity": full_equity,
            "position_budget": allocation, "selected": selected, "candidates": rows,
            "cash_weight": 1 - sum(decision.target_weights.values(), D(0)), "decision": decision,
            "orders_submitted": 0,
            "limitations": ["Liquidity uses close times volume as a turnover proxy",
                            "Metadata and spread must have trustworthy effective/publication times",
                            "Targets and affordability are research estimates, not executable orders"]}


class SelectionEngine:
    def __init__(self, securities: SecurityHistory, policy: SelectionPolicy, costs: Costs):
        self.securities, self.policy, self.costs = securities, policy, costs

    def __call__(self, spec, view, valid_until, wallet: Wallet):
        marks = {symbol: h[-1].close for symbol, h in view.histories.items() if h and h[-1].session == view.expected_session}
        equity = wallet.equity(marks)
        return select_candidates(spec, view, self.securities, self.policy, self.costs, valid_until, equity, audit=False)["decision"]

    def execution_allowed(self, symbol: str, side: str, at: datetime) -> bool:
        state = self.securities.states(at).get(symbol)
        if state is None or state.status != "NORMAL" or (at - state.effective_at).total_seconds() > self.policy.max_state_age_seconds:
            return False
        return side == "SELL" or (not any((state.managed, state.caution, state.leveraged, state.inverse))
                                  and state.asset_type in self.policy.allowed_types and state.market in self.policy.allowed_markets)
