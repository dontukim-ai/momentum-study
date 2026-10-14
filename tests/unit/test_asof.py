from datetime import date

import polars as pl
import pytest

from momentum.asof import AsOfFrame, LookaheadError


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "security_id": ["A", "A", "B"],
        "session_date": [date(2000, 1, 3), date(2000, 1, 4), date(2000, 1, 4)],
        "close_raw": [10.0, 11.0, 5.0],
        "available_at": [date(2000, 1, 3), date(2000, 1, 4), date(2000, 1, 6)],
    })


def test_visible_at_hides_rows_not_yet_available():
    out = AsOfFrame(_frame()).visible_at(date(2000, 1, 4))
    assert out.height == 2
    assert "B" not in out["security_id"].to_list()


def test_row_dated_earlier_but_published_later_is_still_hidden():
    f = _frame().with_columns(pl.lit(date(2000, 1, 10)).alias("available_at"))
    assert AsOfFrame(f).visible_at(date(2000, 1, 4)).height == 0


def test_missing_available_at_column_is_rejected_at_construction():
    with pytest.raises(LookaheadError):
        AsOfFrame(_frame().drop("available_at"))


def test_raw_escape_hatch_is_counted_for_audit():
    a = AsOfFrame(_frame())
    a.raw()
    assert a.raw_access_count == 1


def test_visible_at_is_inclusive_of_t_itself():
    out = AsOfFrame(_frame()).visible_at(date(2000, 1, 3))
    assert out.height == 1
    assert out["security_id"].to_list() == ["A"]


def test_visible_at_returns_empty_frame_with_full_columns_when_nothing_visible():
    out = AsOfFrame(_frame()).visible_at(date(1999, 1, 1))
    assert out.height == 0
    assert out.columns == _frame().columns


def test_asof_frame_is_defended_from_later_mutation_of_the_caller_s_frame():
    original = _frame()
    a = AsOfFrame(original)
    original.drop_in_place("close_raw")
    assert "close_raw" in a.raw().columns
    assert "close_raw" in a.visible_at(date(2000, 1, 6)).columns


def test_visible_at_does_not_mutate_and_calls_are_independent():
    a = AsOfFrame(_frame())
    out1 = a.visible_at(date(2000, 1, 3))
    out2 = a.visible_at(date(2000, 1, 6))
    assert out1.height == 1
    assert out2.height == 3
    assert a.raw().height == 3
