from __future__ import annotations

import json

import polars as pl
import pytest

from momentum.cli import HoldoutLockedError, main


def test_h4_runs_end_to_end_on_the_synthetic_market(tmp_path) -> None:
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
    for f in ("orders.csv", "fills.csv", "positions.csv", "cash.csv", "equity.csv", "run.json"):
        assert (tmp_path / f).exists()


def test_every_buy_fill_joins_a_signals_row_at_the_same_asof(tmp_path) -> None:
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
    fills = pl.read_csv(tmp_path / "fills.csv")
    signals = pl.read_csv(tmp_path / "signals.csv")
    buys = fills.filter(pl.col("side") == "buy")
    assert buys.height > 0
    assert buys.join(signals, on=["security_id", "asof"], how="anti").height == 0


def test_every_sell_fill_was_selected_by_the_signal_at_some_earlier_asof(tmp_path) -> None:
    # A sell's provenance is NOT "the signal picked it today" -- it is "the
    # signal picked it once, and has since stopped picking it." signals.csv
    # only ever records what the signal actually produced (no rows are
    # invented to make a join succeed), so a sell is traceable iff its
    # security appears in signals.csv at an asof strictly before the sell's
    # own asof.
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
    fills = pl.read_csv(
        tmp_path / "fills.csv",
        schema_overrides={"asof": pl.Date},
    )
    signals = pl.read_csv(
        tmp_path / "signals.csv",
        schema_overrides={"asof": pl.Date},
    )
    sells = fills.filter(pl.col("side") == "sell")
    assert sells.height > 0
    earliest_signal_asof = signals.group_by("security_id").agg(
        pl.col("asof").min().alias("first_asof")
    )
    joined = sells.join(earliest_signal_asof, on="security_id", how="left")
    untraceable = joined.filter(
        pl.col("first_asof").is_null() | (pl.col("first_asof") >= pl.col("asof"))
    )
    assert untraceable.height == 0


def test_holdout_phase_is_refused_without_a_frozen_manifest(tmp_path) -> None:
    with pytest.raises(HoldoutLockedError):
        main(["run", "--hypothesis", "H4", "--phase", "holdout", "--out", str(tmp_path)])


def test_run_json_records_hashes_and_holdout_access_flag(tmp_path) -> None:
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
    meta = json.loads((tmp_path / "run.json").read_text())
    assert set(meta) >= {
        "run_id",
        "phase",
        "config_hash",
        "data_hash",
        "code_commit",
        "seed",
        "started_at",
        "accessed_holdout",
    }
    assert meta["accessed_holdout"] is False
