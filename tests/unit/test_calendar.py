from datetime import date

from momentum.calendar import TradingCalendar

CAL = TradingCalendar.from_dates([
    date(2000, 6, 28), date(2000, 6, 29), date(2000, 6, 30),  # 금 = 월말 = 주말
    date(2000, 7, 3), date(2000, 7, 5),                        # 7/4 휴장
])


def test_next_session_skips_holidays():
    assert CAL.next_session(date(2000, 7, 3)) == date(2000, 7, 5)


def test_next_session_at_the_end_of_data_is_none():
    assert CAL.next_session(date(2000, 7, 5)) is None


def test_month_end_uses_last_session_not_calendar_last_day():
    assert CAL.is_month_end(date(2000, 6, 30)) is True
    assert CAL.is_month_end(date(2000, 6, 29)) is False


def test_week_end_is_the_last_session_of_the_week():
    assert CAL.is_week_end(date(2000, 6, 30)) is True
    assert CAL.is_week_end(date(2000, 6, 29)) is False


def test_rebalance_dates_are_observation_dates_and_fill_is_the_next_session():
    obs = CAL.rebalance_dates("monthly", date(2000, 6, 1), date(2000, 7, 31))
    assert obs == [date(2000, 6, 30)]
    assert CAL.next_session(obs[0]) == date(2000, 7, 3)


# --- Authorized additions beyond the brief ---


def test_non_session_date_is_not_month_end_or_week_end():
    # 2000-07-01 (Sat) and 07-02 (Sun) are not sessions in CAL.
    saturday = date(2000, 7, 1)
    assert CAL.is_month_end(saturday) is False
    assert CAL.is_week_end(saturday) is False
    # next_session on a non-session date returns the first session strictly after it.
    assert CAL.next_session(saturday) == date(2000, 7, 3)


def test_year_boundary_month_end_and_iso_week_semantics():
    # Month boundary that is also a year boundary: Dec 29 (Fri) is the last
    # session of both December and (ISO) week 52, 1999.
    year_cal = TradingCalendar.from_dates([
        date(1999, 12, 28),  # Tue, week 1999-W52
        date(1999, 12, 29),  # Wed, week 1999-W52 (last session of Dec & that week)
        date(1999, 12, 30),  # Thu, week 1999-W52... actually verify below
        date(2000, 1, 3),    # Mon, next ISO week
    ])
    # 1999-12-30 is a Thursday; ISO week of 1999-12-28,29,30 is the same week
    # (1999-W52), and 2000-01-03 (Mon) starts ISO week 2000-W01. So the last
    # session before the week rolls over is 1999-12-30, not 12-29.
    assert year_cal.is_week_end(date(1999, 12, 29)) is False
    assert year_cal.is_week_end(date(1999, 12, 30)) is True
    # Month end: December's last session in this list is 1999-12-30.
    assert year_cal.is_month_end(date(1999, 12, 30)) is True
    assert year_cal.is_month_end(date(1999, 12, 29)) is False
    # A week spanning the year boundary should not be split by "week of year"
    # resetting: 2000-01-03 is a new ISO week distinct from 1999-W52.
    assert year_cal.next_session(date(1999, 12, 30)) == date(2000, 1, 3)


def test_sessions_inclusive_and_sorted_even_with_unsorted_input():
    unsorted_cal = TradingCalendar.from_dates([
        date(2000, 7, 5), date(2000, 6, 28), date(2000, 7, 3), date(2000, 6, 30),
        date(2000, 6, 29),
    ])
    result = unsorted_cal.sessions(date(2000, 6, 28), date(2000, 7, 5))
    assert result == [
        date(2000, 6, 28), date(2000, 6, 29), date(2000, 6, 30),
        date(2000, 7, 3), date(2000, 7, 5),
    ]


def test_rebalance_dates_daily_returns_every_session_and_all_freqs_are_sessions():
    start, end = date(2000, 6, 1), date(2000, 7, 31)
    daily = CAL.rebalance_dates("daily", start, end)
    assert daily == CAL.sessions(start, end)

    all_sessions = set(CAL.sessions(start, end))
    for freq in ("daily", "weekly", "monthly"):
        for d in CAL.rebalance_dates(freq, start, end):
            assert d in all_sessions
