"""run.json's own internal bookkeeping, and its shape against the canonical
``runs`` schema.

The ``executed + skipped == observed`` relationship currently holds by
construction (the single chronological loop in ``run_backtest`` partitions
``obs_dates`` completely into ``obs_to_exec`` and ``skipped_observations``).
That is exactly why it needs a test of its own: the zero-sells bug fixed in
the previous review round also held "by construction" of the old code,
right up until it did not. This reads a real ``run.json`` from disk rather
than reconstructing the partition inline, so it also covers serialization.
"""

from __future__ import annotations

import json
from pathlib import Path

from momentum.cli import main
from momentum.data.schema import validate
from momentum.runs.registry import to_runs_row


def _run(tmp_path: Path) -> dict:
    main(
        [
            "run",
            "--hypothesis",
            "H4",
            "--phase",
            "development",
            "--data",
            "synth:seed=7",
            "--out",
            str(tmp_path),
        ]
    )
    return json.loads((tmp_path / "run.json").read_text())


def test_executed_plus_skipped_equals_observed(tmp_path: Path) -> None:
    meta = _run(tmp_path)
    assert meta["rebalances_executed"] + len(meta["skipped_observations"]) == meta[
        "observation_dates"
    ]


def test_every_skip_carries_a_non_empty_reason(tmp_path: Path) -> None:
    meta = _run(tmp_path)
    for skip in meta["skipped_observations"]:
        assert isinstance(skip.get("reason"), str)
        assert skip["reason"] != ""


def test_run_json_validates_against_the_runs_schema(tmp_path: Path) -> None:
    meta = _run(tmp_path)
    row = to_runs_row(meta)
    validate("runs", row)  # must not raise
