"""Hypothesis parameters (``top_pct``, ``require_positive``, H3's
``secondary_filter`` turnover percentage) must come from
``config/hypotheses.yaml`` -- not from a module-level constant that happens
to match the yaml today. Proven by writing a fixture config directory with
a different ``top_pct`` and checking the number of selected names actually
changes, for all four hypotheses.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from momentum.asof import AsOfFrame
from momentum.signals import REGISTRY
from momentum.signals.base import CONFIG_DIR as REAL_CONFIG_DIR

ASOF = date(2001, 6, 29)
PRIOR = date(2001, 5, 31)


def _write_fixture_config(tmp_path: Path, top_pct: float) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "universe.yaml").write_text("{}\n")
    (tmp_path / "execution.yaml").write_text("{}\n")
    (tmp_path / "validation.yaml").write_text(
        "windows:\n"
        "  development: {start: 2000-01-03, end: 2001-12-31}\n"
    )
    (tmp_path / "hypotheses.yaml").write_text(
        "hypotheses:\n"
        f"  - {{id: H1, top_pct: {top_pct}, require_positive: true,  rebalance: daily,   lookback: 1d}}\n"
        f"  - {{id: H2, top_pct: {top_pct}, require_positive: true,  rebalance: weekly,  lookback: 1w}}\n"
        f"  - {{id: H3, top_pct: {top_pct}, require_positive: true,  rebalance: monthly, lookback: 1mo,\n"
        "     secondary_filter: turnover_top_30pct}\n"
        f"  - {{id: H4, top_pct: {top_pct}, require_positive: false, rebalance: monthly, lookback: 12mo, skip: 1mo}}\n"
    )
    return tmp_path


def _universe(ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "effective_date": [ASOF] * len(ids),
            "security_id": ids,
            "adv_usd": [50_000_000.0] * len(ids),
            "rank": list(range(len(ids))),
            "included": [True] * len(ids),
            "exclusion_reason": [None] * len(ids),
        }
    )


def _h1_h2_prices(ids: list[str]) -> AsOfFrame:
    # H1 needs T and T-1; give every security a positive one-day return so
    # none is dropped by the positive-return filter.
    t_minus_1 = date(2001, 6, 28)
    rows = []
    for i, sid in enumerate(ids):
        rows.append({"security_id": sid, "session_date": t_minus_1, "close_raw": 100.0, "available_at": t_minus_1})
        rows.append({"security_id": sid, "session_date": ASOF, "close_raw": 100.0 + i * 0.01 + 1.0, "available_at": ASOF})
    return AsOfFrame(pl.DataFrame(rows))


def test_top_pct_from_a_fixture_config_changes_h1_selection_count(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from momentum.signals import base

    ids = [f"SEC{i:04d}" for i in range(20)]
    prices = _h1_h2_prices(ids)
    universe = _universe(ids)

    fixture_dir = _write_fixture_config(tmp_path, top_pct=0.10)
    monkeypatch.setattr(base, "CONFIG_DIR", fixture_dir)
    base._load_hypothesis_cfg.cache_clear()
    rt_10pct = REGISTRY["H1"].rank(ASOF, universe, prices)

    fixture_dir2 = _write_fixture_config(tmp_path / "v2", top_pct=0.50)
    monkeypatch.setattr(base, "CONFIG_DIR", fixture_dir2)
    rt_50pct = REGISTRY["H1"].rank(ASOF, universe, prices)

    assert rt_10pct.frame.height == 2  # int(20 * 0.10)
    assert rt_50pct.frame.height == 10  # int(20 * 0.50)
    assert rt_10pct.frame.height != rt_50pct.frame.height

    monkeypatch.setattr(base, "CONFIG_DIR", REAL_CONFIG_DIR)
    base._load_hypothesis_cfg.cache_clear()


def test_require_positive_false_from_config_lets_h4_keep_negative_scores(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from momentum.signals import base

    (tmp_path / "validation.yaml").write_text(
        "windows:\n"
        "  development: {start: 2000-01-03, end: 2001-12-31}\n"
    )
    (tmp_path / "hypotheses.yaml").write_text(
        "hypotheses:\n"
        "  - {id: H4, top_pct: 0.10, require_positive: true, rebalance: monthly, lookback: 12mo, skip: 1mo}\n"
    )
    monkeypatch.setattr(base, "CONFIG_DIR", tmp_path)
    base._load_hypothesis_cfg.cache_clear()

    ids = [f"SEC{i:04d}" for i in range(20)]
    rows = []
    den = date(2000, 6, 30)
    num = date(2001, 5, 31)
    for i, sid in enumerate(ids):
        den_px = 100.0
        num_px = 90.0 - i * 0.01  # every security loses money
        rows.append({"security_id": sid, "session_date": den, "close_raw": den_px, "available_at": den})
        rows.append({"security_id": sid, "session_date": num, "close_raw": num_px, "available_at": num})
    prices = AsOfFrame(pl.DataFrame(rows))
    universe = _universe(ids)

    rt = REGISTRY["H4"].rank(date(2001, 6, 29), universe, prices)
    # require_positive flipped to true via config -- H4 must now drop every
    # negative-scoring name, even though its own docstring/module constant
    # used to assume require_positive is always false.
    assert rt.frame.height == 0

    monkeypatch.setattr(base, "CONFIG_DIR", REAL_CONFIG_DIR)
    base._load_hypothesis_cfg.cache_clear()
