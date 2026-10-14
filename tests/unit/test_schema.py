from datetime import date

import pytest

from momentum.config import SpecUndefinedError
from momentum.data.schema import SCHEMAS, validate
from momentum.synth import generate_market


def test_all_six_spec_tables_are_defined():
    assert set(SCHEMAS) == {
        "security_master", "daily_prices", "corporate_actions",
        "shares_history", "calendar", "runs",
    }


def test_synthetic_market_conforms_to_the_schema():
    m = generate_market(seed=7, n_securities=5, start=date(2000, 1, 3), end=date(2000, 3, 31))
    validate("daily_prices", m.daily_prices)
    validate("security_master", m.security_master)
    validate("shares_history", m.shares_history)


def test_all_five_synth_frames_conform_to_schema():
    m = generate_market(seed=11, n_securities=8, start=date(2000, 1, 3), end=date(2000, 6, 30))
    validate("security_master", m.security_master)
    validate("daily_prices", m.daily_prices)
    validate("corporate_actions", m.corporate_actions)
    validate("shares_history", m.shares_history)
    validate("calendar", m.calendar)


def test_validate_unknown_table_raises_spec_undefined_error():
    m = generate_market(seed=1, n_securities=2, start=date(2000, 1, 3), end=date(2000, 1, 31))
    with pytest.raises(SpecUndefinedError):
        validate("not_a_table", m.daily_prices)


def test_validate_missing_column_raises_spec_undefined_error():
    m = generate_market(seed=1, n_securities=2, start=date(2000, 1, 3), end=date(2000, 1, 31))
    with pytest.raises(SpecUndefinedError):
        validate("daily_prices", m.daily_prices.drop("close_raw"))


def test_validate_wrong_dtype_raises_spec_undefined_error():
    m = generate_market(seed=1, n_securities=2, start=date(2000, 1, 3), end=date(2000, 1, 31))
    bad = m.daily_prices.with_columns(m.daily_prices["close_raw"].cast(str).alias("close_raw"))
    with pytest.raises(SpecUndefinedError):
        validate("daily_prices", bad)
