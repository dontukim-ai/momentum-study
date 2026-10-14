"""Canonical column schemas for the project's six minimum-data tables (spec §13).

Five of the six (``security_master``, ``daily_prices``, ``corporate_actions``,
``shares_history``, ``calendar``) are produced by ``momentum.synth`` today;
dtypes here are taken directly from what ``synth.py`` actually constructs
(read there, not guessed) so that ``validate`` exercises the real contract
between the generator and everything downstream. The sixth, ``runs``, is
written by Task 10 and has no producer yet; its dtypes are this task's best
reasonable guess at the natural type of each field named in the spec.

``validate`` never coerces. A missing column, an unknown table name, or a
column whose dtype does not match is a ``SpecUndefinedError`` — the same
error Task 3 uses for every other "the spec didn't say, so refuse instead of
guessing" situation in this codebase.
"""

from __future__ import annotations

import polars as pl

from momentum.config import SpecUndefinedError

SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "security_master": {
        "security_id": pl.Utf8(),
        "ticker": pl.Utf8(),
        "exchange": pl.Utf8(),
        "share_type": pl.Utf8(),
        "effective_from": pl.Date(),
        "effective_to": pl.Date(),
        "available_at": pl.Date(),
    },
    "daily_prices": {
        "security_id": pl.Utf8(),
        "session_date": pl.Date(),
        "open_raw": pl.Float64(),
        "high_raw": pl.Float64(),
        "low_raw": pl.Float64(),
        "close_raw": pl.Float64(),
        "volume": pl.Float64(),
        "currency": pl.Utf8(),
        "available_at": pl.Date(),
        "source_id": pl.Utf8(),
    },
    "corporate_actions": {
        "security_id": pl.Utf8(),
        "action_type": pl.Utf8(),
        "ex_date": pl.Date(),
        "pay_date": pl.Date(),
        "split_ratio": pl.Float64(),
        "cash_amount": pl.Float64(),
        "available_at": pl.Date(),
    },
    "shares_history": {
        "security_id": pl.Utf8(),
        "effective_date": pl.Date(),
        "shares_outstanding": pl.Float64(),
        "unit": pl.Utf8(),
        "available_at": pl.Date(),
    },
    "calendar": {
        "session_date": pl.Date(),
        # Nullable Datetime("us"): synth.py's current placeholder rows are
        # all-None but explicitly cast to this dtype (not left to polars'
        # Null-from-all-None inference), so the contract is the real
        # calendar timestamp type Task 5 will fill in, not today's
        # placeholder value. Time unit is explicit (microseconds) so a
        # future writer using a different unit (e.g. milliseconds) fails
        # loudly instead of silently.
        "open_utc": pl.Datetime("us"),
        "close_utc": pl.Datetime("us"),
        "exchange": pl.Utf8(),
    },
    # `runs` is written by Task 10's `momentum.cli.run_backtest` /
    # `momentum.runs.registry`. ``observation_dates`` and
    # ``rebalances_executed`` are plain counts. ``skipped_observations`` is
    # a *list* of ``{"asof": ..., "reason": ...}`` records, not a scalar --
    # this table (like every table `validate()` checks) is a flat columnar
    # shape, so it is stored here as a JSON-encoded string column (the same
    # representation a real warehouse table would use for a variable-length
    # nested field it isn't worth a child table for), never silently
    # flattened to just a count. ``momentum.runs.registry.to_runs_row``
    # builds a row in exactly this shape from a run record.
    "runs": {
        "run_id": pl.Utf8(),
        "phase": pl.Utf8(),
        "config_hash": pl.Utf8(),
        "data_hash": pl.Utf8(),
        "code_commit": pl.Utf8(),
        "seed": pl.Int64(),
        "started_at": pl.Datetime("us", "UTC"),
        "accessed_holdout": pl.Boolean(),
        "observation_dates": pl.Int64(),
        "rebalances_executed": pl.Int64(),
        "skipped_observations": pl.Utf8(),
    },
}


def validate(name: str, df: pl.DataFrame) -> None:
    """Validate ``df`` against the canonical schema for table ``name``.

    Raises ``SpecUndefinedError`` for an unknown table name, a column
    missing from ``df``, or a column whose dtype does not match — never
    coerces.
    """
    schema = SCHEMAS.get(name)
    if schema is None:
        raise SpecUndefinedError(f"'{name}' 은 정의된 표가 아니다. SCHEMAS: {sorted(SCHEMAS)}")

    for col, expected_dtype in schema.items():
        if col not in df.columns:
            raise SpecUndefinedError(f"'{name}' 표에 필수 컬럼 '{col}' 이 없다.")
        actual_dtype = df.schema[col]
        if actual_dtype != expected_dtype:
            raise SpecUndefinedError(
                f"'{name}.{col}' 의 타입이 {actual_dtype} 이다 (기대: {expected_dtype})."
            )
