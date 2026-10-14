"""Shared contract every hypothesis signal implements.

A ``Signal`` returns a rank table -- it never places orders, never touches
the ledger, and never re-implements execution or accounting (CLAUDE.md rule
7: hypotheses share one engine, they only differ in how they rank
securities).
"""

from __future__ import annotations

from datetime import date
from functools import cache
from pathlib import Path
from typing import NamedTuple, Protocol

import polars as pl

from momentum.asof import AsOfFrame
from momentum.config import HypothesisCfg, SpecUndefinedError, load_config

#: Same config directory cli.py uses -- one level deeper on disk (this file
#: is src/momentum/signals/base.py, not src/momentum/cli.py), hence
#: parents[3] rather than parents[2]. A plain module attribute (not frozen
#: at import) so tests can monkeypatch it to point at a fixture config dir.
CONFIG_DIR: Path = Path(__file__).resolve().parents[3] / "config"

#: Columns of ``RankTable.frame`` -- a run's ``signals.csv`` output uses the
#: same shape (spec §13 requires the signal be recorded independently of any
#: particular test).
RANK_TABLE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "hypothesis_id": pl.Utf8,
    "security_id": pl.Utf8,
    "asof": pl.Date,
    "score": pl.Float64,
    "rank": pl.Int64,
}


class RankTable(NamedTuple):
    """One hypothesis's ranked selections as of one observation date.

    ``frame`` has columns ``hypothesis_id, security_id, asof, score, rank``,
    already cut down to the selected top slice -- callers do not re-apply
    ``top_pct`` themselves. ``unavailable`` (with a human-readable
    ``reason``) signals "this hypothesis could not rank anything today"
    (e.g. an empty eligible universe) as distinct from "it ranked and
    nothing qualified," which is simply an empty (but available) frame.
    """

    frame: pl.DataFrame
    unavailable: bool = False
    reason: str | None = None


class Signal(Protocol):
    def rank(self, asof: date, universe: pl.DataFrame, prices: AsOfFrame) -> RankTable: ...


REGISTRY: dict[str, Signal] = {}


@cache
def _load_hypothesis_cfg(config_dir: str, hypothesis_id: str) -> HypothesisCfg:
    cfg = load_config(Path(config_dir))
    for h in cfg.hypotheses.hypotheses:
        if h.id == hypothesis_id:
            return h
    raise SpecUndefinedError(
        f"no config/hypotheses.yaml entry for hypothesis {hypothesis_id!r}"
    )


def hypothesis_config(hypothesis_id: str) -> HypothesisCfg:
    """This hypothesis's ``top_pct`` / ``require_positive`` /
    ``secondary_filter`` / etc., read from ``config/hypotheses.yaml`` --
    never a module-level constant a signal invents on its own. Editing the
    yaml must actually change behaviour (the frozen-config discipline this
    project is built on); a hardcoded constant that happens to match the
    yaml today would silently stop matching it the moment someone edits the
    file. Cached per ``(CONFIG_DIR, hypothesis_id)`` pair so a daily-cadence
    backtest does not re-parse the yaml on every single rebalance -- tests
    that monkeypatch ``CONFIG_DIR`` to a fixture directory still get a fresh
    read because the cache key includes it.
    """
    return _load_hypothesis_cfg(str(CONFIG_DIR), hypothesis_id)
