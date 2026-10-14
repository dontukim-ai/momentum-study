"""Deterministic synthetic market generator.

Real US-equity market data has not been acquired for this project and may
never be. This module is the primary data source for every downstream task:
it produces a fully self-consistent synthetic market (prices, corporate
actions, shares outstanding, security master, trading calendar) from a seed,
and it is reused in Task 13 to build the null-hypothesis market used to
calibrate the statistical tests.

Invariant — per-security price paths are independent of ``n_securities``:
each security draws from its own child RNG stream, keyed by its own index in
the requested id list, spawned from ``seed`` via ``SeedSequence.spawn``.
Spawned child seeds depend only on the spawning index, not on how many
children are spawned in total, so security k's entire simulated price path
(level draws, daily walk, shares outstanding) is bit-identical whether it is
generated as part of a call with ``n_securities=5`` or ``n_securities=20``
(same ``seed``, ``start``, ``end``). Later tasks may rely on this: markets
generated at different universe sizes from the same seed are directly
comparable security-by-security, not just internally self-consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

# Column dtypes for corporate_actions are declared explicitly (rather than
# left to polars' row-list inference) for two reasons:
#   1. with_events=False produces zero corporate-action rows; inferring a
#      schema from an empty list of dicts yields a column-less frame, which
#      breaks any downstream code that selects e.g. "action_type" (Task 13
#      builds exactly such a null market).
#   2. split_ratio is None on dividend rows and cash_amount is None on split
#      rows. Inferring dtypes from a mixed float/None row list can produce
#      Null-typed or otherwise non-numeric columns instead of nullable
#      Float64 columns.
_CORPORATE_ACTIONS_SCHEMA: dict[str, type[pl.DataType]] = {
    "security_id": pl.Utf8,
    "action_type": pl.Utf8,
    "ex_date": pl.Date,
    "pay_date": pl.Date,
    "split_ratio": pl.Float64,
    "cash_amount": pl.Float64,
    "available_at": pl.Date,
}


@dataclass(frozen=True)
class SynthMarket:
    security_master: pl.DataFrame
    daily_prices: pl.DataFrame
    corporate_actions: pl.DataFrame
    shares_history: pl.DataFrame
    calendar: pl.DataFrame


def _sessions(start: date, end: date) -> list[date]:
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:  # 휴일은 Task 5 달력에서 정교화
            out.append(d)
        d += timedelta(days=1)
    return out


def generate_market(
    *, seed: int, n_securities: int, start: date, end: date, with_events: bool = True
) -> SynthMarket:
    sessions = _sessions(start, end)
    ids = [f"SEC{i:04d}" for i in range(n_securities)]
    delisted = set(ids[: max(1, n_securities // 10)]) if with_events else set()
    delist_on = {sid: sessions[len(sessions) // 2] for sid in delisted}

    # Each security draws from its own independent child RNG stream (spawned
    # from the single seed) rather than sharing one global generator drawn in
    # loop order. This keeps the per-security, per-day price trajectory
    # identical between with_events=True and with_events=False: with a shared
    # generator, truncating one security's draws early (due to delisting)
    # would shift every later security's draws out of sync between the two
    # modes, silently breaking their comparability as null/alt markets.
    child_seeds = np.random.SeedSequence(seed).spawn(max(n_securities, 1))
    security_rngs = [np.random.default_rng(s) for s in child_seeds[:n_securities]]

    px_rows: list[dict[str, Any]] = []
    ca_rows: list[dict[str, Any]] = []
    sh_rows: list[dict[str, Any]] = []
    sm_rows: list[dict[str, Any]] = []
    for k, sid in enumerate(ids):
        rng = security_rngs[k]
        level = 20.0 + 80.0 * rng.random()
        shares = float(rng.integers(20_000_000, 400_000_000))
        end_s = delist_on.get(sid)
        for d in sessions:
            if end_s is not None and d > end_s:
                break
            level = max(1.0, level * (1.0 + float(rng.normal(0.0004, 0.02))))
            px_rows.append({
                "security_id": sid, "session_date": d,
                "open_raw": level * (1 + float(rng.normal(0, 0.003))),
                "high_raw": level * 1.01, "low_raw": level * 0.99,
                "close_raw": level, "volume": float(rng.integers(500_000, 8_000_000)),
                "currency": "USD", "available_at": d, "source_id": "synth",
            })
            sh_rows.append({
                "security_id": sid, "effective_date": d, "shares_outstanding": shares,
                "unit": "share", "available_at": d,
            })
        if with_events and k % 5 == 0:
            ex = sessions[len(sessions) // 3]
            ca_rows.append({
                "security_id": sid, "action_type": "split", "ex_date": ex, "pay_date": ex,
                "split_ratio": 2.0, "cash_amount": None,
                "available_at": ex - timedelta(days=10),
            })
        if with_events and k % 3 == 0:
            ex = sessions[len(sessions) // 4]
            ca_rows.append({
                "security_id": sid, "action_type": "cash_dividend", "ex_date": ex,
                "pay_date": ex + timedelta(days=14), "split_ratio": None, "cash_amount": 0.25,
                "available_at": ex - timedelta(days=10),
            })
        sm_rows.append({
            "security_id": sid, "ticker": sid, "exchange": "XNYS", "share_type": "common",
            "effective_from": sessions[0], "effective_to": end_s, "available_at": sessions[0],
        })

    cal = pl.DataFrame({
        "session_date": sessions,
        "open_utc": [None] * len(sessions),
        "close_utc": [None] * len(sessions),
        "exchange": ["XNYS"] * len(sessions),
    }).with_columns(
        pl.col("open_utc").cast(pl.Datetime("us")),
        pl.col("close_utc").cast(pl.Datetime("us")),
    )
    return SynthMarket(
        pl.DataFrame(sm_rows), pl.DataFrame(px_rows),
        pl.DataFrame(ca_rows, schema=_CORPORATE_ACTIONS_SCHEMA),
        pl.DataFrame(sh_rows), cal,
    )
