"""tools/intraday_forward.py: forward record of holdout winners from 2026-10-26 (synthetic bars)."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_intraday_holdout as th  # synthetic KIS days and the holdout module

spec = importlib.util.spec_from_file_location("intraday_forward", ROOT / "tools/intraday_forward.py")
fwd = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = fwd
spec.loader.exec_module(fwd)


def setup(tmp_path, passed=True):
    hold_days = th.weekdays(date(2026, 9, 21), 25)          # ends 2026-10-23
    fwd_days = th.weekdays(date(2026, 10, 26), 8)
    bars = tmp_path / "bars"
    th.write_bars(bars, th.UNIVERSE, hold_days + fwd_days)
    manifest = tmp_path / "manifest.json"
    th.holdout.freeze(["C5-A 069500"], th.UNIVERSE, manifest)
    record = tmp_path / "holdout-run.json"
    record.write_text(json.dumps({"manifest_sha256": th.holdout._sha(manifest),
                                  "rows": [{"name": "C5-A 069500", "passed": passed}]}))
    return manifest, record, bars, fwd_days


def test_forward_records_only_days_from_the_start_with_comparisons(tmp_path):
    manifest, record, bars, fwd_days = setup(tmp_path)
    result = fwd.run(manifest, record, bars, end="2026-12-31")
    row = result["rows"][0]
    assert result["days"] == [fwd_days[0], fwd_days[-1]] and row["days"] == len(fwd_days)
    assert min(t["day"] for t in result["trades"]["C5-A 069500"]) >= "2026-10-26"
    assert set(row["report_only"]) == {"intraday_only", "full_day"} and "passed" not in row
    assert result["benchmark_069500_95"]["days"] == len(fwd_days)


def test_end_cuts_the_record(tmp_path):
    manifest, record, bars, fwd_days = setup(tmp_path)
    assert fwd.run(manifest, record, bars, end=fwd_days[3])["days"] == [fwd_days[0], fwd_days[3]]


def test_only_candidates_that_passed_the_holdout_are_recorded(tmp_path):
    manifest, record, bars, _ = setup(tmp_path, passed=False)
    with pytest.raises(SpecUndefinedError, match="no candidate passed"):
        fwd.run(manifest, record, bars, end="2026-12-31")


def test_the_holdout_record_must_belong_to_the_manifest(tmp_path):
    manifest, record, bars, _ = setup(tmp_path)
    record.write_text(json.dumps({"manifest_sha256": "0" * 64, "rows": [{"name": "C5-A 069500", "passed": True}]}))
    with pytest.raises(SpecUndefinedError, match="another manifest"):
        fwd.run(manifest, record, bars, end="2026-12-31")
    with pytest.raises(SpecUndefinedError, match="holdout has not run"):
        fwd.run(manifest, tmp_path / "missing.json", bars, end="2026-12-31")
