from datetime import date

from momentum.synth import generate_market

KW = {"seed": 7, "n_securities": 20, "start": date(2000, 1, 3), "end": date(2000, 6, 30)}


def test_same_seed_gives_identical_frames():
    assert generate_market(**KW).daily_prices.equals(generate_market(**KW).daily_prices)


def test_market_includes_required_lifecycle_events():
    m = generate_market(**KW)
    assert {"split", "cash_dividend"} <= set(m.corporate_actions["action_type"].to_list())
    assert m.security_master.filter(m.security_master["effective_to"].is_not_null()).height >= 1


def test_no_price_rows_after_delisting():
    m = generate_market(**KW)
    sm = m.security_master.filter(m.security_master["effective_to"].is_not_null())
    for sid, last in zip(sm["security_id"], sm["effective_to"]):
        rows = m.daily_prices.filter(
            (m.daily_prices["security_id"] == sid) & (m.daily_prices["session_date"] > last)
        )
        assert rows.height == 0


def test_corporate_actions_are_available_before_their_ex_date():
    m = generate_market(**KW)
    ca = m.corporate_actions
    assert (ca["available_at"] <= ca["ex_date"]).all()


def test_no_events_market_has_full_corporate_actions_schema_but_zero_rows():
    m = generate_market(**{**KW, "with_events": False})
    ca = m.corporate_actions
    assert ca.height == 0
    assert set(ca.columns) == {
        "security_id",
        "action_type",
        "ex_date",
        "pay_date",
        "split_ratio",
        "cash_amount",
        "available_at",
    }


def test_events_do_not_perturb_rng_draw_order():
    # with_events also controls delisting, so the two markets can have different
    # numbers of rows for delisted securities (the False market never truncates
    # them). What must hold is that for any (security_id, session_date) present
    # in BOTH markets, the simulated price is bit-identical -- i.e. turning
    # events on/off does not shift the RNG draw order security-to-security.
    with_events = generate_market(**{**KW, "with_events": True}).daily_prices
    without_events = generate_market(**{**KW, "with_events": False}).daily_prices
    joined = with_events.join(
        without_events,
        on=["security_id", "session_date"],
        how="inner",
        suffix="_no_events",
    )
    assert joined.height > 0
    assert joined["close_raw"].equals(joined["close_raw_no_events"])


def test_security_price_path_is_independent_of_n_securities():
    # Security k's price path must not shift depending on how many
    # securities the call requests -- otherwise markets generated at
    # different universe sizes from the same seed would not be comparable
    # security-by-security, only internally self-consistent.
    small = generate_market(**{**KW, "n_securities": 5})
    large = generate_market(**{**KW, "n_securities": 20})

    small_px = small.daily_prices.filter(small.daily_prices["security_id"] == "SEC0002")
    large_px = large.daily_prices.filter(large.daily_prices["security_id"] == "SEC0002")

    assert small_px.height > 0
    assert small_px["close_raw"].equals(large_px["close_raw"])
    assert small_px["session_date"].equals(large_px["session_date"])


def test_corporate_action_optional_columns_are_nullable_float():
    m = generate_market(**KW)
    ca = m.corporate_actions
    assert ca["split_ratio"].dtype == m.corporate_actions.schema["split_ratio"]
    assert str(ca["split_ratio"].dtype) == "Float64"
    assert str(ca["cash_amount"].dtype) == "Float64"

    split_rows = ca.filter(ca["action_type"] == "split")
    assert split_rows["cash_amount"].is_null().all()

    dividend_rows = ca.filter(ca["action_type"] == "cash_dividend")
    assert dividend_rows["split_ratio"].is_null().all()
