from __future__ import annotations

from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

Phase = Literal["development", "walkforward", "holdout"]


class SpecUndefinedError(RuntimeError):
    """명세에 없는 값을 추정하지 않고 즉시 중단한다."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UniverseCfg(Strict):
    min_close_usd: float = 5.0
    min_adv_usd: float = 20_000_000
    adv_lookback_days: int = 60
    min_history_days: int = 252
    max_names: int = 1000


class ExecutionCfg(Strict):
    base_cost_bps: float = 10.0
    stress_cost_bps: float = 25.0
    optimistic_cost_bps: float = 5.0
    participation_limit: float = 0.001
    participation_lookback_days: int = 20
    account_equity_usd: int = 30000
    max_positions: int = 50
    max_weight: float = 0.05
    min_positions_for_full_investment: int = 20
    cash_buffer: float = 0.005
    cash_interest_rate: float = 0.0


class Window(Strict):
    start: date
    end: date


class ValidationCfg(Strict):
    windows: dict[str, Window]
    bootstrap_block_days: int = 63
    bootstrap_block_sensitivity: list[int] = [21, 126]
    bootstrap_iterations: int = 10_000
    seed: int = 20260916
    alpha: float = 0.05


class HypothesisCfg(Strict):
    id: str
    top_pct: float
    require_positive: bool
    rebalance: Literal["daily", "weekly", "monthly"]
    lookback: str
    skip: str | None = None
    secondary_filter: str | None = None


class HypothesesCfg(Strict):
    hypotheses: list[HypothesisCfg]


class Config(Strict):
    universe: UniverseCfg
    execution: ExecutionCfg
    validation: ValidationCfg
    hypotheses: HypothesesCfg
    hash: str


def load_config(config_dir: Path) -> Config:
    raw: dict[str, dict] = {}
    hasher = sha256()
    for name in sorted(("universe", "execution", "validation", "hypotheses")):
        filename = f"{name}.yaml"
        p = Path(config_dir) / filename
        hasher.update(filename.encode("utf-8"))
        hasher.update(b"\x00")
        if not p.exists():
            hasher.update(b"<missing>")
            hasher.update(b"\x00")
            raw[name] = {}
            continue
        content = p.read_bytes()
        hasher.update(content)
        hasher.update(b"\x00")
        loaded: object = yaml.safe_load(content.decode("utf-8"))
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise SpecUndefinedError(
                f"{filename}: top-level document must be a mapping, got "
                f"{type(loaded).__name__}"
            )
        raw[name] = loaded
    digest = hasher.hexdigest()
    try:
        return Config(
            universe=UniverseCfg(**raw["universe"]),
            execution=ExecutionCfg(**raw["execution"]),
            validation=ValidationCfg(**raw["validation"]),
            hypotheses=HypothesesCfg(**raw["hypotheses"]),
            hash=digest,
        )
    except ValidationError as e:
        raise SpecUndefinedError(str(e)) from e
