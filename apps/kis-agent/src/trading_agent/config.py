"""Strict non-secret configuration; only local simulated execution is supported."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .contracts import AgentError, Costs, D, StrategySpec, dec


@dataclass(frozen=True)
class Settings:
    strategy: StrategySpec = StrategySpec()
    costs: Costs = Costs()
    initial_cash: Decimal = D("10000000")
    lookbacks: tuple[int, ...] = (5, 10, 20)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings()
    try:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError:
        raise AgentError("Invalid TOML configuration") from None
    allowed = {"runtime": {"mode", "live_enabled", "initial_cash"},
               "strategy": {"lookback", "top_n", "gross_weight", "min_momentum"},
               "costs": {"commission_bps", "sell_tax_bps", "slippage_bps"},
               "research": {"lookbacks"}}
    if set(raw) - allowed.keys():
        raise AgentError("Unknown configuration section")
    for section, values in raw.items():
        if not isinstance(values, dict) or set(values) - allowed[section]:
            raise AgentError("Unknown configuration keys in " + section)
    runtime = raw.get("runtime", {})
    if runtime.get("mode", "simulated") != "simulated" or runtime.get("live_enabled", False) is not False:
        raise AgentError("This MVP supports simulated local execution only; live orders are not implemented")
    strategy = dict(raw.get("strategy", {}))
    for key in ("gross_weight", "min_momentum"):
        if key in strategy:
            strategy[key] = dec(strategy[key])
    costs = {k: dec(v) for k, v in raw.get("costs", {}).items()}
    initial = dec(runtime.get("initial_cash", "10000000"))
    if initial <= 0:
        raise AgentError("Initial local-demo cash must be positive")
    return Settings(StrategySpec(**strategy), Costs(**costs), initial,
                    tuple(raw.get("research", {}).get("lookbacks", (5, 10, 20))))
