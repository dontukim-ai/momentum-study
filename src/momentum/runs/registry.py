"""Run metadata: the ``run.json`` record every backtest run writes.

Kept separate from ``cli.py`` so the shape of a run record (and its
validation against the canonical ``runs`` schema in
``momentum.data.schema``) is testable and reusable without pulling in the
whole CLI/argparse surface.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from momentum.config import Phase

REPO_ROOT = Path(__file__).resolve().parents[3]


def code_commit() -> str:
    """Best-effort current commit SHA; ``"unknown"`` outside a git checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def new_run_id() -> str:
    return uuid.uuid4().hex


def build_run_record(
    *,
    phase: Phase,
    config_hash: str,
    data_hash: str,
    seed: int,
    accessed_holdout: bool,
) -> dict[str, Any]:
    """Assemble the ``run.json`` record for one backtest run.

    ``run_id`` and ``started_at`` are the only fields that legitimately
    differ between two otherwise-identical runs (see
    ``tests/unit/test_determinism.py``): everything else is a pure function
    of the run's inputs.
    """
    return {
        "run_id": new_run_id(),
        "phase": phase,
        "config_hash": config_hash,
        "data_hash": data_hash,
        "code_commit": code_commit(),
        "seed": seed,
        "started_at": datetime.now(UTC).isoformat(),
        "accessed_holdout": accessed_holdout,
    }


def write_run_record(out: Path, record: dict[str, Any]) -> Path:
    path = out / "run.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True))
    return path


def to_runs_row(record: dict[str, Any]) -> pl.DataFrame:
    """One row for the ``runs`` table shape in ``momentum.data.schema.SCHEMAS``.

    The canonical ``runs`` table is a flat columnar shape, like every table
    ``validate()`` checks; ``skipped_observations`` in a run record is a
    *list* of ``{"asof", "reason"}`` records, which has no honest scalar
    column representation. Rather than silently flattening it to just a
    count (which would throw away which observations were skipped and why),
    it is JSON-encoded into a single string column here -- the same
    representation a real warehouse table would use for a variable-length
    nested field not worth a child table of its own.
    """
    return pl.DataFrame(
        {
            "run_id": [record["run_id"]],
            "phase": [record["phase"]],
            "config_hash": [record["config_hash"]],
            "data_hash": [record["data_hash"]],
            "code_commit": [record["code_commit"]],
            "seed": [record["seed"]],
            "started_at": [record["started_at"]],
            "accessed_holdout": [record["accessed_holdout"]],
            "observation_dates": [record["observation_dates"]],
            "rebalances_executed": [record["rebalances_executed"]],
            "skipped_observations": [json.dumps(record["skipped_observations"])],
        },
        schema={
            "run_id": pl.Utf8,
            "phase": pl.Utf8,
            "config_hash": pl.Utf8,
            "data_hash": pl.Utf8,
            "code_commit": pl.Utf8,
            "seed": pl.Int64,
            "started_at": pl.Utf8,
            "accessed_holdout": pl.Boolean,
            "observation_dates": pl.Int64,
            "rebalances_executed": pl.Int64,
            "skipped_observations": pl.Utf8,
        },
    ).with_columns(
        pl.col("started_at").str.to_datetime(time_unit="us", time_zone="UTC")
    )
