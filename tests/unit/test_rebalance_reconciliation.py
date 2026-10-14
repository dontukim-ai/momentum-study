"""Scheduled-vs-executed rebalance count reconciliation, for all four
hypotheses. ``run.json`` records ``observation_dates``,
``rebalances_executed`` and ``skipped_observations`` (Task 10); every
difference between scheduled and executed rebalances must be accounted for
by a logged skip reason -- never simply vanish.

H1 rebalances DAILY over the full 2000-2012 development window (~3,270
observation dates, vs. H4's ~156 monthly ones), which makes its end-to-end
run too slow for the default suite; it is marked ``slow`` and excluded from
the default `pytest -q` run (see ``pyproject.toml``'s
``addopts = "-m 'not slow'"``) -- run it explicitly with
``uv run pytest -m slow``.
"""

from __future__ import annotations

import json

import pytest

from momentum.cli import main


def _run_and_load(tmp_path, hypothesis: str) -> dict:
    main(
        [
            "run",
            "--hypothesis",
            hypothesis,
            "--phase",
            "development",
            "--data",
            "synth:seed=7",
            "--out",
            str(tmp_path),
        ]
    )
    return json.loads((tmp_path / "run.json").read_text())


def _assert_reconciles(meta: dict) -> None:
    scheduled = meta["observation_dates"]
    executed = meta["rebalances_executed"]
    skipped = meta["skipped_observations"]
    assert scheduled - executed == len(skipped)
    assert executed > 0
    for row in skipped:
        assert row["reason"]  # every gap carries a non-empty reason


@pytest.mark.parametrize("hypothesis", ["H2", "H3", "H4"])
def test_scheduled_and_actual_rebalance_counts_differ_only_for_logged_reasons(
    tmp_path, hypothesis: str
) -> None:
    meta = _run_and_load(tmp_path, hypothesis)
    _assert_reconciles(meta)


@pytest.mark.slow
def test_h1_scheduled_and_actual_rebalance_counts_differ_only_for_logged_reasons(tmp_path) -> None:
    meta = _run_and_load(tmp_path, "H1")
    _assert_reconciles(meta)
    # Roughly one rebalance per trading day over 13 years -- confirms this
    # actually ran the daily cadence, not a silently-shrunk window.
    assert meta["observation_dates"] > 3000
