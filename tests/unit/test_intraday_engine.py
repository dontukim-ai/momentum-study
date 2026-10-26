"""tools/intraday_engine.py: the common intraday engine (intraday-dev-plan-v11 §2)."""

import csv
import importlib.util
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("intraday_engine", ROOT / "tools/intraday_engine.py")
engine = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = engine  # dataclasses look their module up
spec.loader.exec_module(engine)

A, B = "AAA001", "BBB002"


def golden_days():
    with (ROOT / "tests/fixtures/intraday_engine_case.csv").open(encoding="utf-8") as handle:
        return engine.days_from_rows(csv.DictReader(handle))


class Scripted:
    """Day 1: buy A 30% / B 20% after 09:00, keep B overnight at 20%; day 2: sell everything at the open."""

    def open_auction(self, view):
        return {} if view.day == "2026-01-06" else None

    def on_minute(self, view):
        if view.day == "2026-01-05" and view.time == "090000":
            return {A: 0.3, B: 0.2}
        return None

    def close_auction(self, view):
        return {B: 0.2} if view.day == "2026-01-05" else None


def test_golden_case_is_reproduced_to_the_won():
    """Hand calculation (commission 1.5bp rounded half up to the won; tick 5 KRW at 2,000+ else 1 KRW).

    Day 1, 2026-01-05, capital 100,000,000.
    - End of 09:00: targets A 30%, B 20% on equity 100,000,000 at the last closes A 10,000 / B 1,500.
      A wants floor(30,000,000 / 10,000) = 3,000; B wants floor(20,000,000 / 1,500) = 13,333.
    - 09:01 A fills at open 10,010 + 5 = 10,015: 3,000 x 10,015 = 30,045,000, commission 4,506.75 -> 4,507.
      Capacity 10% x close 10,020 x volume 100,000 = 100,200,000 is not binding. B has no 09:01 bar (no trade).
    - 09:02 B fills at its next bar's open 1,510 + 1 = 1,511. Capacity 10% x 1,512 x 100,000 = 15,120,000,
      so floor(15,120,000 / 1,511) = 10,006 shares: 15,119,066, commission 2,267.8599 -> 2,268.
      The other 3,327 shares are dropped (no resting order).
      Cash 100,000,000 - 30,045,000 - 4,507 - 15,119,066 - 2,268 = 54,829,159.
    - Close auction, decided on information through 15:19 (last closes A 10,040, B 1,512):
      equity 54,829,159 + 3,000 x 10,040 + 10,006 x 1,512 = 100,078,231.
      A is not targeted: sell 3,000 at the 15:30 close 10,100 (no tick) = 30,300,000, commission 4,545.
      B 20%: floor(20,015,646.2 / 1,512) = 13,237, buy 3,231 at 1,520 = 4,911,120, commission 736.668 -> 737.
      Cash 54,829,159 + 30,300,000 - 4,545 - 4,911,120 - 737 = 80,212,757.
      Equity 80,212,757 + 13,237 x 1,520 = 100,332,997; day P&L +332,997. B is held overnight.
    Day 2, 2026-01-06.
    - Open auction (empty targets): sell B 13,237 at the 09:00 open 1,530 (no tick) = 20,252,610,
      capacity 10% x 1,531 x 200,000 = 30,620,000; commission 3,037.8915 -> 3,038.
      Cash 80,212,757 + 20,252,610 - 3,038 = 100,462,329 = equity; day P&L +129,332
      (= 13,237 x (1,530 - 1,520) - 3,038).
    Totals: commission 4,507 + 2,268 + 4,545 + 737 + 3,038 = 15,095;
    tick cost 3,000 x 5 + 10,006 x 1 = 25,006.
    """
    result = engine.run(golden_days(), Scripted())
    trades = [(t["day"], t["time"], t["kind"], t["symbol"], t["side"], t["quantity"], t["price"], t["commission"])
              for t in result.trades]
    assert trades == [
        ("2026-01-05", "090100", "continuous", A, "BUY", 3000, D(10015), D(4507)),
        ("2026-01-05", "090200", "continuous", B, "BUY", 10006, D(1511), D(2268)),
        ("2026-01-05", "153000", "close_auction", A, "SELL", 3000, D(10100), D(4545)),
        ("2026-01-05", "153000", "close_auction", B, "BUY", 3231, D(1520), D(737)),
        ("2026-01-06", "090000", "open_auction", B, "SELL", 13237, D(1530), D(3038)),
    ]
    cut = [e for e in result.events if e["event"] == "capacity_cut"]
    assert [(e["symbol"], e["requested"], e["filled"]) for e in cut] == [(B, 13333, 10006)]
    assert [(d["day"], d["end_equity"], d["pnl"]) for d in result.daily] == [
        ("2026-01-05", D(100332997), D(332997)), ("2026-01-06", D(100462329), D(129332))]
    assert result.final_equity == D(100462329) and result.cash == D(100462329) and result.positions == {}
    assert (result.commission_total, result.tick_cost_total) == (D(15095), D(25006))


class Spy:
    """Records what each call could see; never trades."""

    def __init__(self):
        self.seen = []

    def _look(self, view, kind):
        for symbol in (A, B):
            times = [b.time for b in view.bars(symbol)]
            assert all(t <= view.time for t in times)
            self.seen.append((kind, view.day, view.time, symbol, times[-1] if times else None))
        assert all(day < view.day for day, _ in view.previous_days())

    def open_auction(self, view):
        self._look(view, "open")

    def on_minute(self, view):
        self._look(view, "minute")

    def close_auction(self, view):
        self._look(view, "close")


def test_the_strategy_never_sees_a_later_bar():
    spy = Spy()
    engine.run(golden_days(), spy)
    day1 = [s for s in spy.seen if s[1] == "2026-01-05"]
    # Before the open nothing of today; at 09:01 B still shows its 09:00 bar; the close decision sees
    # information through 15:19 only, never the 15:30 bar.
    assert ("open", "2026-01-05", "085959", A, None) in day1
    assert ("minute", "2026-01-05", "090100", B, "090000") in day1
    assert ("close", "2026-01-05", "151959", A, "090200") in day1
    assert not any(s[2] >= "152000" and s[0] == "minute" for s in spy.seen)


def test_tick_size_rule():
    assert engine.tick(D(1999)) == D(1) and engine.tick(D(2000)) == D(5) and engine.tick(D(86000)) == D(5)


@pytest.mark.parametrize("targets", [{A: 0.6}, {A: 0.5, B: 0.6}, {A: -0.1}, {"ZZZ999": 0.1}])
def test_weights_outside_the_caps_are_rejected(targets):
    class Bad(Scripted):
        def on_minute(self, view):
            return targets

    with pytest.raises(SpecUndefinedError):
        engine.run(golden_days(), Bad())


def test_auction_fills_pay_no_tick_and_continuous_fills_do():
    result = engine.run(golden_days(), Scripted())
    ticks = {(t["kind"], t["symbol"]): t["tick_cost"] for t in result.trades}
    assert ticks[("continuous", A)] == D(15000) and ticks[("continuous", B)] == D(10006)
    assert all(t["tick_cost"] == 0 for t in result.trades if t["kind"] != "continuous")


def test_unfilled_orders_expire_at_1520_and_close_auction_flattens_by_default():
    class Late(Scripted):
        def on_minute(self, view):
            # B wants in after 09:02, but B has no later continuous bar: the order expires.
            return {B: 0.1} if view.time == "090200" and view.day == "2026-01-05" else None

        def close_auction(self, view):
            return None

    result = engine.run(golden_days(), Late())
    assert [e["event"] for e in result.events if e["symbol"] == B][:1] == ["expired"]
    assert result.trades == [] and result.final_equity == D(100000000)
