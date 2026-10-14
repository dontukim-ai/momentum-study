"""Fast regression test for the daily-cadence observe-after-execute bug
fixed in this task: under a "daily" rebalance frequency, an observation
day's own ``exec_date`` is the very next session -- which is itself another
observation day. Reading ``ledger.positions`` for that day's NEW targets
before that day's OWN scheduled fills (from yesterday's observation) are
booked computes targets against stale holdings and can schedule a sell for
shares that are about to vanish (or already have), eventually raising
``SpecUndefinedError: sell of N exceeds M held``.

The only other test that exercises this path end-to-end
(``test_h1_scheduled_and_actual_rebalance_counts_differ_only_for_logged_reasons``)
runs H1 over the FULL 2000-2012 development window and is marked ``slow``
(excluded from the default suite). This test proves the same bug on a
handful of sessions by monkeypatching ``load_config`` to shrink the
"development" window to two weeks, so it belongs in the default suite and
would have caught a revert of the reorder immediately.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from momentum import cli
from momentum.config import Window, load_config

# Empirically the smallest window (seed=7, n_securities=20) that reproduces
# the bug this fixes: at 357 days the old ordering raises
# "SpecUndefinedError: sell of 63 exceeds 0 held in SEC0006"; below that the
# particular selections/holdings in this seed never happen to collide. 360
# days is used for a small margin. This was found by bisection against the
# UNFIXED loop ordering (see task-11-report.md) -- confirmed to fail there
# and to pass against the fixed ordering below.
_WINDOW_DAYS = 360


@pytest.fixture
def narrow_dev_window(monkeypatch: pytest.MonkeyPatch) -> None:
    real_cfg = load_config(cli.CONFIG_DIR)
    start = date(2000, 1, 3)
    narrow = Window(start=start, end=start + timedelta(days=_WINDOW_DAYS))
    new_windows = dict(real_cfg.validation.windows)
    new_windows["development"] = narrow
    new_validation = real_cfg.validation.model_copy(update={"windows": new_windows})
    new_cfg = real_cfg.model_copy(update={"validation": new_validation})
    monkeypatch.setattr(cli, "load_config", lambda config_dir: new_cfg)


def test_h1_daily_cadence_does_not_oversell_when_an_observation_day_is_also_an_execution_day(
    narrow_dev_window: None, tmp_path: Path
) -> None:
    # Every daily-cadence session after the first is BOTH the execution day
    # for the prior observation AND its own new observation day -- that
    # condition is present from day 2 onward. What takes ~360 days (not 2)
    # to actually manifest as an oversell is this specific seed's holdings
    # drifting into a state where a stale-holdings target collides with a
    # security that has since been fully sold; see _WINDOW_DAYS above.
    cli.main(
        [
            "run",
            "--hypothesis",
            "H1",
            "--phase",
            "development",
            "--data",
            "synth:seed=7,n_securities=20",
            "--out",
            str(tmp_path),
        ]
    )
    assert (tmp_path / "run.json").exists()
