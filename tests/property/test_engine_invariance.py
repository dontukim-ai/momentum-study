"""Property test: the execution/accounting engine does not know or care
which hypothesis produced a rank table (CLAUDE.md rule 7).

Given the SAME sequence of ranked security lists, driving the SAME shared
pipeline (``compute_targets`` -> ``execute`` -> ``Ledger.step_session`` --
exactly the functions ``momentum.cli.run_backtest`` calls) once labelled
"H1" and once labelled "H4" must produce byte-identical ledgers: same fills,
same positions, same cash, same NAV, every session. The ``hypothesis_id``
value is threaded into the ``RankTable`` built for each run (proving the two
runs really are distinguishable "as H1" and "as H4" rank tables, not merely
two identical function calls) but is never read by any of the three shared
functions -- if a future change forked the engine per hypothesis (e.g. an
``if hypothesis_id == "H1":`` branch inside ``compute_targets`` or
``Ledger``), the two runs below would diverge and this test would fail.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import polars as pl
import pytest

from momentum.config import ExecutionCfg
from momentum.execution.engine import execute
from momentum.ledger.ledger import Ledger
from momentum.portfolio.targets import compute_targets
from momentum.signals.base import RANK_TABLE_SCHEMA, RankTable

# Three monthly rebalances over four securities -- selections deliberately
# change month to month (drops, adds, re-adds) so the run exercises sells,
# buys, and a reselected/held name, not just a single static basket.
ASOFS = [date(2001, 1, 31), date(2001, 2, 28), date(2001, 3, 30)]
EXEC_DATES = [date(2001, 2, 1), date(2001, 3, 1), date(2001, 4, 2)]
SELECTIONS: dict[date, list[str]] = {
    ASOFS[0]: ["SEC0000", "SEC0001", "SEC0002"],
    ASOFS[1]: ["SEC0001", "SEC0002", "SEC0003"],  # SEC0000 dropped, SEC0003 added
    ASOFS[2]: ["SEC0001", "SEC0003"],  # SEC0002 dropped, SEC0001/SEC0003 held
}

# Deterministic prices: prior close used for sizing at each asof, opens used
# to fill at each exec date.
PREV_CLOSE = {
    ASOFS[0]: {"SEC0000": Decimal(50), "SEC0001": Decimal(80), "SEC0002": Decimal(30)},
    ASOFS[1]: {"SEC0001": Decimal(82), "SEC0002": Decimal(31), "SEC0003": Decimal(60)},
    ASOFS[2]: {"SEC0001": Decimal(85), "SEC0003": Decimal(62)},
}
OPENS = {
    EXEC_DATES[0]: {"SEC0000": Decimal("50.5"), "SEC0001": Decimal("80.5"), "SEC0002": Decimal("30.2")},
    EXEC_DATES[1]: {"SEC0001": Decimal("82.1"), "SEC0002": Decimal("31.3"), "SEC0003": Decimal("60.4"), "SEC0000": Decimal(51)},
    EXEC_DATES[2]: {"SEC0001": Decimal("85.2"), "SEC0003": Decimal("62.5"), "SEC0002": Decimal(32)},
}
CLOSES = {
    EXEC_DATES[0]: {"SEC0000": Decimal("50.6"), "SEC0001": Decimal("80.7"), "SEC0002": Decimal("30.3")},
    EXEC_DATES[1]: {"SEC0001": Decimal("82.4"), "SEC0002": Decimal("31.5"), "SEC0003": Decimal("60.6"), "SEC0000": Decimal("51.1")},
    EXEC_DATES[2]: {"SEC0001": Decimal("85.4"), "SEC0003": Decimal("62.7"), "SEC0002": Decimal("32.1")},
}
ADV = {
    date_: {sid: 5_000_000.0 for sid in opens_}
    for date_, opens_ in OPENS.items()
}


def _rank_table(hypothesis_id: str, asof: date) -> RankTable:
    ids = SELECTIONS[asof]
    return RankTable(
        frame=pl.DataFrame(
            {
                "hypothesis_id": [hypothesis_id] * len(ids),
                "security_id": ids,
                "asof": [asof] * len(ids),
                "score": [1.0 - 0.01 * i for i in range(len(ids))],
                "rank": list(range(len(ids))),
            },
            schema=RANK_TABLE_SCHEMA,
        )
    )


def run_with(hypothesis_id: str) -> Ledger:
    """Drive the SAME shared pipeline cli.py uses, fed by rank tables
    tagged with ``hypothesis_id`` -- the only thing that differs between
    two calls of this function with different ids."""
    cfg = ExecutionCfg()
    ledger = Ledger(starting_cash=Decimal(30_000), cfg=cfg)

    for asof, exec_date in zip(ASOFS, EXEC_DATES):
        rank_table = _rank_table(hypothesis_id, asof)
        ranked_ids = rank_table.frame.sort("rank")["security_id"].to_list()

        holdings = dict(ledger.positions)
        targets = compute_targets(ranked_ids, ledger.nav, PREV_CLOSE[asof], holdings, cfg)

        fills = execute(
            targets,
            OPENS[exec_date],
            ADV[exec_date],
            ledger.cash,
            cfg,
            session_date=exec_date,
            asof=asof,
        )
        ledger.apply_fills(fills, exec_date)
        ledger.mark_to_market(CLOSES[exec_date], exec_date)

    return ledger


def _fills_as_rows(ledger: Ledger) -> list[tuple]:
    # Every Fill field except nothing -- the fill carries no hypothesis_id
    # at all, which is itself part of what this test is proving.
    return [tuple(f) for f in ledger.fills]


def test_identical_rank_tables_produce_identical_ledgers_across_hypotheses() -> None:
    h1_ledger = run_with("H1")
    h4_ledger = run_with("H4")

    assert _fills_as_rows(h1_ledger) == _fills_as_rows(h4_ledger)
    assert h1_ledger.positions == h4_ledger.positions
    assert h1_ledger.cash == h4_ledger.cash
    assert h1_ledger.nav == h4_ledger.nav
    assert h1_ledger.marks == h4_ledger.marks


def test_the_rank_tables_actually_differ_only_by_hypothesis_id() -> None:
    """Sanity check that this is a real test, not a vacuous one: the two
    runs really were fed RankTables tagged differently."""
    for asof in ASOFS:
        h1_rt = _rank_table("H1", asof)
        h4_rt = _rank_table("H4", asof)
        assert h1_rt.frame["hypothesis_id"].to_list() != h4_rt.frame["hypothesis_id"].to_list()
        assert h1_rt.frame.drop("hypothesis_id").equals(h4_rt.frame.drop("hypothesis_id"))


@pytest.mark.parametrize("hypothesis_id", ["H1", "H2", "H3", "H4"])
def test_every_registered_hypothesis_id_drives_the_same_ledger(hypothesis_id: str) -> None:
    """All four hypothesis labels -- not just H1 and H4 -- must be
    interchangeable engine inputs."""
    baseline = run_with("H1")
    other = run_with(hypothesis_id)
    assert _fills_as_rows(other) == _fills_as_rows(baseline)
    assert other.nav == baseline.nav
