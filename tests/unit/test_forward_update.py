"""tools/forward_update.py: the guards around the monthly input update (no network)."""

import argparse
import importlib.util
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("forward_update_tool", ROOT / "tools/forward_update.py")
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = tool  # dataclasses look their module up
spec.loader.exec_module(tool)


def kst(text):
    return datetime.fromisoformat(text).replace(tzinfo=tool.KST)


def test_quotes_count_as_published_from_14_kst_of_the_next_weekday():
    friday = date(2026, 11, 27)
    assert tool.next_weekday(friday) == date(2026, 11, 30)
    assert not tool.published(friday, kst("2026-11-30T13:59"))
    assert tool.published(friday, kst("2026-11-30T14:00"))
    # The same instant in another zone is judged in KST.
    assert tool.published(friday, datetime.fromisoformat("2026-11-30T05:00+00:00"))


def write_day(fsc, kind, day, total):
    path = fsc / kind / day
    path.mkdir(parents=True)
    (path / "fetch.json").write_text(json.dumps({"requested_date": day, "total_count": total, "pages": []}))


def test_dart_periods_continue_after_the_last_saved_one(tmp_path):
    lists = tmp_path / "dart_list" / "I"
    for name in ("2026-07-05_2026-10-04", "2026-10-05_2026-10-20"):
        (lists / name).mkdir(parents=True)
        (lists / name / "fetch.json").write_text("{}")
    (lists / "2026-10-21_2026-11-30").mkdir()  # unfinished: no fetch.json
    assert tool.dart_start(tmp_path, "I", date(2000, 1, 1)) == date(2026, 10, 21)
    assert tool.dart_start(tmp_path, "A", date(2026, 10, 21)) == date(2026, 10, 21)


def test_empty_weekdays_after_the_previous_end_are_listed(tmp_path):
    fsc = tmp_path / "fsc"
    for day, total in (("2026-10-21", 2800), ("2026-10-22", 0), ("2026-10-23", 2801)):
        write_day(fsc, "stock", day, total)
    write_day(fsc, "etf", "2026-10-21", 900)
    assert tool.empty_weekdays(fsc, date(2026, 10, 20), date(2026, 10, 23)) == {
        "stock": ["2026-10-22"], "etf": []}
    assert tool.day_total(fsc, "etf", date(2026, 10, 22)) is None


def write_list(path, header, rows):
    path.write_bytes(("\n".join([",".join(header), *(",".join(r) for r in rows)]) + "\n").encode("cp949"))


def kind_dir(path, *, delisting=True):
    path.mkdir(parents=True)
    write_list(path / "a.csv", ["번호", "종목코드", "최초지정일"], [["1", "000001", "2026/10/01"]])
    write_list(path / "b.csv", ["번호", "종목코드", "정지일"], [["1", "000002", "2026/11/02"]])
    if delisting:
        write_list(path / "c.csv", ["번호", "종목코드", "증권구분", "폐지일"], [["1", "000003", "주권", "2026/11/10"]])
    return path


def test_kind_files_take_only_delisting_lists_from_history(tmp_path):
    new, old = kind_dir(tmp_path / "new"), kind_dir(tmp_path / "old")
    files = tool.kind_files(new, old)
    assert files == {"managed": [new / "a.csv"], "halted": [new / "b.csv"],
                     "delisted": [new / "c.csv", old / "c.csv"]}
    with pytest.raises(SpecUndefinedError, match="exactly one"):
        tool.kind_files(kind_dir(tmp_path / "short", delisting=False), old)


def test_price_digest_ignores_order_and_later_sessions(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    a.write_text("session,symbol\n2026-10-20,X\n2026-10-19,Y\n")
    b.write_text("session,symbol\n2026-10-19,Y\n2026-10-20,X\n2026-10-21,Z\n")
    assert tool.session_digest(a, date(2026, 10, 20)) == tool.session_digest(b, date(2026, 10, 20))
    b.write_text("session,symbol\n2026-10-19,Y\n2026-10-20,W\n")
    assert tool.session_digest(a, date(2026, 10, 20)) != tool.session_digest(b, date(2026, 10, 20))


def test_merged_actions_refuse_a_duplicate(tmp_path):
    one = {"security_id": "A", "action_type": "cash_dividend", "ex_date": "2026-12-28", "evidence": {}}
    (tmp_path / "x.json").write_text(json.dumps([one]))
    (tmp_path / "y.json").write_text(json.dumps([{**one, "action_type": "split"}]))
    counts = tool.merge_actions([tmp_path / "x.json", tmp_path / "y.json"], tmp_path / "out.json")
    assert counts == {"cash_dividend": 1, "split": 1}
    assert all("evidence" not in a for a in json.loads((tmp_path / "out.json").read_text()))
    with pytest.raises(SpecUndefinedError, match="duplicate"):
        tool.merge_actions([tmp_path / "x.json", tmp_path / "x.json"], tmp_path / "out.json")


def arguments(tmp_path, end="2026-11-30", **changes):
    previous = tmp_path / "previous"
    previous.mkdir(exist_ok=True)
    (previous / "sessions.json").write_text(json.dumps(["2026-10-19", "2026-10-20"]))
    kind = tmp_path / "kind-new"
    if not kind.exists():
        kind_dir(kind)
    values = {"end": date.fromisoformat(end), "kind": kind, "kind_history": tmp_path / "none",
              "previous": previous, "raw": tmp_path / "raw", "out_root": tmp_path / "fwd9", "dry_run": True}
    return argparse.Namespace(**{**values, **changes})


def test_update_guards_stop_before_any_collection(tmp_path):
    after = kst("2026-12-01T14:00")
    with pytest.raises(SpecUndefinedError, match="public only from 14:00 KST 2026-12-01"):
        tool.update(arguments(tmp_path), kst("2026-12-01T09:00"))
    with pytest.raises(SpecUndefinedError, match="weekday after"):
        tool.update(arguments(tmp_path, "2026-10-20"), after)
    with pytest.raises(SpecUndefinedError, match="weekday after"):
        tool.update(arguments(tmp_path, "2026-11-29"), after)
    (tmp_path / "fwd9" / "2026-11-30").mkdir(parents=True)
    with pytest.raises(SpecUndefinedError, match="never overwritten"):
        tool.update(arguments(tmp_path), after)


def test_dry_run_prints_every_step_and_writes_nothing(tmp_path, capsys):
    out = tool.update(arguments(tmp_path), kst("2026-12-01T14:00"))
    assert not out.exists()
    steps = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("== ")]
    assert steps == ["1-fsc-stock", "1-fsc-etf", "2-dart-I", "2-dart-B", "2-dart-A", "3a-calendar", "3c-adjust",
                     "3d-split", "4a-dividends", "4b-companies", "4c-capital", "4d-divactions", "5-securities",
                     "6a-prices", "6b-screen", "7b-benchmark", "8-quality", "9a-shares", "9b-fin-collect",
                     "9c-fin-build", "10-record"]
