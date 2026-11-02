"""tools/fdrop_study.py (v30) on synthetic inputs: v29's drop, split by the day's f terciles."""

import importlib.util
import sys
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_drop_study as td
import test_ma_cross as t

from momentum.agent.market import KRX
from momentum.krx.dart_quarters import quarter_ends

spec = importlib.util.spec_from_file_location("fdrop_study", ROOT / "tools/fdrop_study.py")
fd = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = fd
spec.loader.exec_module(fd)

N, K = td.N, td.K
FLAT = [100.0 * (1.005 if j % 2 else 0.995) for j in range(N)]


def quarters(day, ratios):
    """Eight quarters ending before ``day``, each public 45 days after its end."""
    ends = quarter_ends(day - timedelta(days=60), 8)
    return [{"period_end": e.isoformat(), "ratio": r,
             "available_at": KRX.disclosure_at(e + timedelta(days=45)).isoformat()} for e, r in zip(ends, ratios, strict=True)]


def steady(scale):
    return [scale * (j % 2) for j in range(8)]


def test_terciles_with_inclusive_cuts():
    values = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert [fd.group(v, values) for v in values] == ["drop_lowf"] * 2 + ["drop_midf"] * 2 + ["drop_highf"] * 2


@pytest.mark.parametrize(("scale", "expected"), [(0.001, "drop_lowf"), (0.05, "drop_midf"), (0.5, "drop_highf")])
def test_the_drop_is_labelled_by_its_f_among_the_days_universe(scale, expected):
    paths = {"005930": td.gap_down(), "000660": FLAT, "035420": FLAT, "051910": FLAT, "005380": FLAT, "069500": [10000] * N}
    data, sessions = t.make_data(paths, n=N)
    day = sessions[K]
    book = {"005930": quarters(day, steady(scale)), "000660": quarters(day, steady(0.01)),
            "035420": quarters(day, steady(0.03)), "051910": quarters(day, steady(0.2)), "005380": quarters(day, steady(0.1))}
    result = fd.study(data, quarters=book)
    counts = {g: e["development"].get("events", 0) for g, e in result["signals"].items()}
    assert counts == {g: int(g == expected) for g in fd.GROUPS}
    assert result["unplaced"] == {"no_fundamentals": 0, "no_tercile": 0}
    assert "edge_h5" in result["signals"]["drop_lowf"] and "edge_h5" not in result["signals"]["drop_highf"]


def test_an_event_without_f_or_a_universe_too_small_is_counted_not_placed(tmp_path):
    data, sessions = t.make_data({"005930": td.gap_down(), "069500": [10000] * N}, n=N)
    assert fd.study(data, quarters={})["unplaced"] == {"no_fundamentals": 1, "no_tercile": 0}
    alone = fd.study(data, quarters={"005930": quarters(sessions[K], steady(0.01))})
    assert alone["unplaced"] == {"no_fundamentals": 0, "no_tercile": 1}
    record = tmp_path / "run.json"
    record.write_text("{}")
    with pytest.raises(SystemExit):
        fd.main(["--input", str(tmp_path), "--events", "e", "--dart-root", str(tmp_path), "--record", str(record)])
