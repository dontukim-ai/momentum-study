from __future__ import annotations

from pathlib import Path


def test_identical_inputs_produce_byte_identical_output(tmp_path: Path) -> None:
    from momentum.cli import run_backtest

    run_backtest(seed=7, phase="development", out=tmp_path / "a")
    run_backtest(seed=7, phase="development", out=tmp_path / "b")
    for name in ("orders.csv", "fills.csv", "positions.csv", "cash.csv", "equity.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
