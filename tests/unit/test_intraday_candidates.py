"""tools/intraday_candidates.py and tools/intraday_dev.py on synthetic minute bars."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dev = load("intraday_dev")
engine, cand = dev.engine, dev.candidates
TIMES = [cand.hhmm(m) for m in range(cand.minutes("090000"), cand.minutes("153000") + 1)]


def day_rows(day, symbol, path=None, volume=100000, opening=None):
    """One bar a minute; ``path`` maps HHMMSS -> close (the last value carries forward)."""
    rows, price = [], 10000
    for t in TIMES:
        price = (path or {}).get(t, price)
        o = opening if (opening and t == "090000") else price
        rows.append({"day": day, "symbol": symbol, "time": t, "open": str(o), "high": str(max(o, price)),
                     "low": str(min(o, price)), "close": str(price), "volume": str(volume)})
    return rows


def run(rows, strategy):
    return engine.run(engine.days_from_rows(rows), strategy)


def trades(result):
    return [(t["day"], t["time"], t["kind"], t["symbol"], t["side"]) for t in result.trades]


def test_there_are_27_named_combinations():
    configs = cand.configs(tuple(f"{i:06d}" for i in range(30)))
    counts = {c: sum(1 for x in configs if x.candidate == c) for c in ("C1", "C2", "C3", "C4", "C5")}
    assert counts == {"C1": 8, "C2": 4, "C3": 4, "C4": 4, "C5": 7}
    assert len({c.name for c in configs}) == 27


def test_c1_buys_the_index_after_a_rising_morning_and_the_inverse_after_a_falling_one():
    rows = []
    for symbol in (cand.KOSPI, cand.KOSPI_INV):
        rows += day_rows("2026-01-05", symbol)
        rows += day_rows("2026-01-06", symbol, {"091000": 10100})       # up by 09:30
        rows += day_rows("2026-01-07", symbol, {"091000": 9900})        # down vs the 01-06 close
    result = run(rows, cand.IntradayMomentum(cand.KOSPI, cand.KOSPI_INV, "150000", False))
    assert trades(result) == [
        ("2026-01-06", "150000", "continuous", cand.KOSPI, "BUY"),
        ("2026-01-06", "153000", "close_auction", cand.KOSPI, "SELL"),
        ("2026-01-07", "150000", "continuous", cand.KOSPI_INV, "BUY"),
        ("2026-01-07", "153000", "close_auction", cand.KOSPI_INV, "SELL"),
    ]


def test_c2_holds_the_etf_that_rose_in_the_same_slot_before():
    rows = []
    for d, day in enumerate(("2026-01-05", "2026-01-06", "2026-01-07")):
        # A rises in the 10:00-10:30 slot each day; B and C are flat.
        # ZZZ009 rises in the 10:00-10:30 slot each day; the others are flat (ties go to AAA001 by name).
        rows += day_rows(day, "ZZZ009", {"100000": 10000 + 100 * d, "100100": 10050 + 100 * d,
                                         "103000": 10100 + 100 * d})
        rows += day_rows(day, "AAA001") + day_rows(day, "BBB002")
    result = run(rows, cand.Periodicity(("AAA001", "BBB002", "ZZZ009"), n=2, k=1))
    zzz = [t for t in trades(result) if t[3] == "ZZZ009"]
    assert zzz[:2] == [("2026-01-07", "100000", "continuous", "ZZZ009", "BUY"),
                       ("2026-01-07", "103000", "continuous", "ZZZ009", "SELL")]
    assert all(t[0] == "2026-01-07" for t in trades(result))  # needs n earlier days


def test_c3_holds_the_worst_last_30_minutes():
    # ZZZ009 falls in the first half hour; the tie-break by name would otherwise pick AAA001.
    rows = day_rows("2026-01-05", "ZZZ009", {"091500": 9500}) + day_rows("2026-01-05", "AAA001")
    result = run(rows, cand.Reversal(("AAA001", "ZZZ009"), k=1, hold=30))
    assert trades(result)[0] == ("2026-01-05", "093000", "continuous", "ZZZ009", "BUY")


def test_c4_buys_a_volume_dip_below_vwap_and_exits_back_at_vwap():
    path = {t: 10000 + (5 if i % 2 else -5) for i, t in enumerate(TIMES)}  # small noise, sigma > 0
    path.update({"110000": 9800, "110100": 9800, "110200": 10050})
    rows = day_rows("2026-01-05", "AAA001", path)
    for row in rows:
        if row["time"] == "110000":
            row["volume"] = "500000"
    result = run(rows, cand.VwapDip(("AAA001",), z=2.0))
    assert trades(result)[:2] == [("2026-01-05", "110100", "continuous", "AAA001", "BUY"),
                                  ("2026-01-05", "110300", "continuous", "AAA001", "SELL")]


def test_c5a_holds_overnight_and_c5b_buys_after_a_gap_down():
    rows = day_rows("2026-01-05", cand.KOSPI) + day_rows("2026-01-06", cand.KOSPI, opening=9850)
    a = run(rows, cand.Overnight((cand.KOSPI,)))
    assert trades(a) == [("2026-01-05", "153000", "close_auction", cand.KOSPI, "BUY"),
                         ("2026-01-06", "090000", "open_auction", cand.KOSPI, "SELL"),
                         ("2026-01-06", "153000", "close_auction", cand.KOSPI, "BUY")]
    b = run(rows, cand.GapReversal(cand.KOSPI, 0.01))
    assert trades(b) == [("2026-01-06", "090100", "continuous", cand.KOSPI, "BUY"),
                         ("2026-01-06", "153000", "close_auction", cand.KOSPI, "SELL")]
    assert trades(run(rows, cand.GapReversal(cand.KOSPI, 0.02))) == []  # a 1.5% gap is not 2%


def test_selection_rule():
    def row(name, candidate, t, days=50, net2=0.01):
        return {"name": name, "candidate": candidate, "t": t, "trade_days": days, "net_return_2x": net2}
    rows = [row("C1 a", "C1", 3.0), row("C1 b", "C1", 2.5), row("C2 a", "C2", 2.2), row("C3 a", "C3", 1.9),
            row("C4 a", "C4", 4.0, days=39), row("C5 a", "C5", 2.1, net2=-0.001)]
    assert dev.select(rows) == ["C1 a", "C2 a"]
    many = [row(f"C{i} x", f"C{i}", 2 + i) for i in range(1, 8)]
    assert dev.select(many) == ["C7 x", "C6 x", "C5 x", "C4 x", "C3 x"]


def test_evaluate_runs_every_combination_and_the_benchmark():
    universe = ("069500", "114800", "229200", "251340", "102110", "278530")
    rows = []
    for day in ("2026-01-05", "2026-01-06"):
        for symbol in universe:
            rows += day_rows(day, symbol)
    result = dev.evaluate(engine.days_from_rows(rows), universe)
    assert result["configs"] == 27 and len(result["rows"]) == 27
    assert result["benchmark_069500_95"] == {"net_return": 0.0, "max_drawdown": 0.0}
    assert result["selected"] == []
