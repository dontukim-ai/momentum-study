"""execute() rounds costs at the ledger's money quantum (whole won for KRW, cents by default)."""

from datetime import date
from decimal import Decimal

from momentum.config import ExecutionCfg
from momentum.execution.costs import one_way_cost
from momentum.execution.engine import execute
from momentum.ledger.ledger import Ledger
from momentum.portfolio.targets import TargetOrder

CFG = ExecutionCfg(base_cost_bps=4.5, participation_limit=1.0)


def test_cents_stay_the_default():
    assert one_way_cost(Decimal(12345), 4.5) == Decimal("5.56")
    fills = execute([TargetOrder("A", "buy", 10, 0)], {"A": Decimal("1234.5")}, {"A": 1e6}, Decimal(100000), CFG,
                    session_date=date(2024, 1, 2), asof=date(2024, 1, 1))
    assert fills[0].cost == Decimal("5.56")


def test_whole_won_costs_keep_a_krw_ledger_exact():
    # 37 x 12,345 = 456,765 won; 4.5bp = 205.54 -> 206 won.
    assert one_way_cost(Decimal(456765), 4.5, Decimal(1)) == Decimal(206)
    ledger = Ledger(Decimal(1_000_000), CFG, money_quantum=Decimal(1))
    ledger.step_session(date(2024, 1, 2), date(2024, 1, 1), [], [TargetOrder("A", "buy", 37, 0)],
                        {"A": Decimal(12345)}, {"A": 1e6}, {"A": Decimal(12400)})
    assert ledger.fills[0].cost == Decimal(206)
    assert ledger.cash == Decimal(1_000_000 - 456765 - 206)
    assert ledger.nav == ledger.cash + 37 * Decimal(12400)
