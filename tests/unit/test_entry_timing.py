"""tools/entry_timing.py and tools/kis_daily_history.py on synthetic series, values by hand."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"tools/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


et = _tool("entry_timing")
kd = _tool("kis_daily_history")


def test_lump_and_instalments_by_hand():
    prices = [100.0] * 21 + [50.0] * 21 + [200.0]
    assert et.lump(prices, 0, 42) == 2.0
    assert et.instalments(prices, 0, 42, 2) == pytest.approx((2.0 + 4.0) / 2)   # half at 100, half at 50


def test_a_steady_rise_favours_the_lump_sum_every_time():
    prices = [1.0005 ** k for k in range(3 * 252 + 30)]
    h = et.horizon(prices, 1)
    assert h["lump_beats_dca6"] == 1.0 and h["lump_beats_dca12"] == 1.0
    assert h["lump"]["median"] == pytest.approx(1.0005 ** 252 - 1) and h["lump"]["loss"] == 0
    assert et.horizon(prices, 5) is None                                            # not enough data


def test_recovery_counts_sessions_until_the_start_close_is_met_again():
    prices = [10.0, 8.0, 9.0, 10.0, 7.0, 11.0]
    r = et.recovery(prices)
    assert r["longest_recovered_sessions"] == 3                                      # 10 -> back to 10 at session 3
    assert r["never_recovered"] == 1 and r["worst_open_sessions"] == 0               # only the last close is open
    falling = et.recovery([10.0 - k * 0.001 for k in range(300)])
    assert falling["within_1y"] == 0.0 and falling["never_recovered"] == 300


class FakeClient:
    def __init__(self, days):
        self.days, self.calls = days, []

    def index_daily(self, code, start, end):
        self.calls.append(end)
        inside = [d for d in self.days if start <= d <= end][-3:][::-1]                # newest first, 3 a read
        return {"output2": [{"stck_bsop_date": d.strftime("%Y%m%d"), "bstp_nmix_prpr": "100.5"} for d in inside]}


def test_collector_walks_back_until_nothing_new_and_never_writes_a_credential(tmp_path):
    days = [date(2003, 1, d) for d in range(2, 10)]
    client = FakeClient(days)
    rows, reads = kd.collect(client, "kospi200", date(1990, 1, 1), date(2003, 1, 31))
    assert [r["date"] for r in rows] == [d.isoformat() for d in days] and reads == 4  # 3 + 3 + 2, then empty
    kd.save(tmp_path / "k.json", "kospi200", rows, reads, ["secret-value"])
    assert json.loads((tmp_path / "k.json").read_text())["rows"][0] == {"date": "2003-01-02", "close": 100.5}
    with pytest.raises(kd.StopCollection):
        kd.save(tmp_path / "x.json", "kospi200", [{"date": "secret-value", "close": 1.0}], 1, ["secret-value"])
    assert not (tmp_path / "x.json").exists()
