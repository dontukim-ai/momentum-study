from datetime import date, timedelta

import polars as pl
import pytest

from momentum.asof import AsOfFrame
from momentum.calendar import TradingCalendar
from momentum.config import UniverseCfg
from momentum.universe import build_universe

CFG = UniverseCfg(min_close_usd=5.0, min_adv_usd=1_000_000, adv_lookback_days=3,
                  min_history_days=3, max_names=2)
DAYS = [date(2000, 6, 26) + timedelta(days=i) for i in range(5)]  # 월~금
EFF = DAYS[-1]
CAL = TradingCalendar.from_dates(DAYS)


def _px(spec: dict[str, tuple[float, float, int]]) -> AsOfFrame:
    """spec: security_id -> (close, volume, n_days_of_history)"""
    rows = []
    for sid, (close, vol, n) in spec.items():
        for d in DAYS[-n:]:
            rows.append({"security_id": sid, "session_date": d, "close_raw": close,
                         "open_raw": close, "volume": vol, "available_at": d})
    return AsOfFrame(pl.DataFrame(rows))


def _master(ids, delisted=(), share_types=None, effective_to_override=None) -> AsOfFrame:
    share_types = share_types or {}
    effective_to_override = effective_to_override or {}
    rows = []
    for s in ids:
        if s in effective_to_override:
            eff_to = effective_to_override[s]
        elif s in delisted:
            eff_to = DAYS[0]
        else:
            eff_to = None
        rows.append({
            "security_id": s, "share_type": share_types.get(s, "common"),
            "effective_from": DAYS[0], "effective_to": eff_to, "available_at": DAYS[0],
        })
    return AsOfFrame(pl.DataFrame(rows))


def _reason(u: pl.DataFrame, sid: str) -> str | None:
    return u.filter(pl.col("security_id") == sid).row(0, named=True)["exclusion_reason"]


def test_penny_stock_is_excluded_with_its_reason():
    spec = {"PENNY": (4.99, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "PENNY") == "min_close_usd"


def test_short_history_is_rejected_before_liquidity_is_even_evaluated():
    spec = {"NEW": (50.0, 1e9, 2), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "NEW") == "min_history_days"


def test_illiquid_name_is_excluded_on_dollar_volume():
    spec = {"THIN": (50.0, 100, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "THIN") == "min_adv_usd"


def test_max_names_keeps_top_adv_and_marks_the_rest():
    spec = {f"S{i}": (50.0, 1e6 * (i + 1), 5) for i in range(4)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert u.filter(pl.col("included")).height == 2
    assert _reason(u, "S0") == "max_names"


def test_delisted_security_is_excluded_but_still_present_in_the_output():
    spec = {"DEAD": (50.0, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec, delisted={"DEAD"}), CAL, CFG, EFF)
    assert _reason(u, "DEAD") == "not_listed"
    assert u.filter(pl.col("security_id") == "DEAD").height == 1


def test_universe_never_sees_rows_published_after_the_effective_date():
    spec = {"OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    px = _px(spec)
    late = px.visible_at(EFF).with_columns([
        pl.lit("LATE").alias("security_id"), pl.lit(EFF + timedelta(days=3)).alias("available_at"),
    ])
    combined = AsOfFrame(pl.concat([px.visible_at(EFF), late]))
    u = build_universe(combined, _master(list(spec) + ["LATE"]), CAL, CFG, EFF)
    assert "LATE" not in u.filter(pl.col("included"))["security_id"].to_list()


def test_adv_uses_the_correct_trailing_sessions_even_when_rows_arrive_shuffled():
    # Regression for the tail()-inside-group_by defect: polars does not
    # guarantee row order within a group_by group, so a positional .tail(n)
    # on unsorted rows can silently average the wrong N sessions. Build a
    # price frame whose rows are deliberately out of session_date order and
    # assert ADV still matches the hand-computed value over the correct
    # trailing sessions (the last adv_lookback_days=3 of the 5 supplied).
    sid = "SHUF"
    closes = {DAYS[0]: 10.0, DAYS[1]: 20.0, DAYS[2]: 30.0, DAYS[3]: 40.0, DAYS[4]: 50.0}
    volumes = {DAYS[0]: 100.0, DAYS[1]: 200.0, DAYS[2]: 300.0, DAYS[3]: 400.0, DAYS[4]: 500.0}
    shuffled_order = [DAYS[2], DAYS[0], DAYS[4], DAYS[1], DAYS[3]]
    rows = [
        {"security_id": sid, "session_date": d, "close_raw": closes[d], "open_raw": closes[d],
         "volume": volumes[d], "available_at": d}
        for d in shuffled_order
    ]
    other = {"OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    px = AsOfFrame(pl.concat([pl.DataFrame(rows), _px(other).visible_at(EFF)]))
    ids = ["SHUF", *other]
    u = build_universe(px, _master(ids), CAL, CFG, EFF)

    # Hand-computed: the last 3 sessions by DATE are DAYS[2], DAYS[3], DAYS[4]
    # -> dollar volumes 30*300=9000, 40*400=16000, 50*500=25000 -> mean 16666.67
    expected_adv = (30.0 * 300 + 40.0 * 400 + 50.0 * 500) / 3
    got_adv = u.filter(pl.col("security_id") == sid).row(0, named=True)["adv_usd"]
    assert got_adv == pytest.approx(expected_adv)


def test_build_universe_never_touches_the_audited_escape_hatch():
    spec = {"OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    prices = _px(spec)
    master = _master(spec)
    build_universe(prices, master, CAL, CFG, EFF)
    assert prices.raw_access_count == 0
    assert master.raw_access_count == 0


def test_output_has_exactly_one_row_per_candidate_security():
    spec = {
        "PENNY": (4.99, 1e6, 5),
        "NEW": (50.0, 1e9, 2),
        "THIN": (50.0, 100, 5),
        "OK1": (50.0, 1e6, 5),
        "OK2": (60.0, 1e6, 5),
    }
    u = build_universe(_px(spec), _master(spec, delisted={"OK2"}), CAL, CFG, EFF)
    assert u.height == len(spec)
    assert set(u["security_id"].to_list()) == set(spec)
    assert u["security_id"].n_unique() == len(spec)


def test_non_common_share_type_is_excluded_with_its_reason():
    # ETF is otherwise a perfectly clean candidate (ample history, well above
    # the close/ADV floors) so the only thing that can exclude it is
    # share_type -- a typo like `!= "Common"` or a swapped comparison would
    # let this security through instead of reporting the reason.
    spec = {"ETF": (50.0, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec, share_types={"ETF": "etf"}), CAL, CFG, EFF)
    assert _reason(u, "ETF") == "share_type"


def test_not_listed_reason_wins_over_share_type_when_both_apply():
    # DEAD is both delisted AND non-common. not_listed is earlier in the
    # priority chain, so it must win even though share_type also matches.
    spec = {"DEAD": (50.0, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(
        _px(spec),
        _master(spec, delisted={"DEAD"}, share_types={"DEAD": "preferred"}),
        CAL, CFG, EFF,
    )
    assert _reason(u, "DEAD") == "not_listed"


def test_share_type_reason_wins_over_min_history_days_when_both_apply():
    # PFD is both non-common AND short-history. share_type is earlier in the
    # priority chain than min_history_days, so it must win.
    spec = {"PFD": (50.0, 1e6, 2), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec, share_types={"PFD": "preferred"}), CAL, CFG, EFF)
    assert _reason(u, "PFD") == "share_type"


def test_security_failing_several_checks_reports_the_earliest_reason():
    # BAD is common and listed (so neither not_listed nor share_type apply)
    # but simultaneously fails all three remaining checks: short history (2
    # days), sub-$5 close, and thin dollar volume. Reordering the chain
    # (e.g. putting min_adv_usd before min_close_usd, or either before
    # min_history_days) would flip this assertion -- proving the priority
    # order is enforced by construction and not just by each fixture
    # happening to trip only one condition.
    #
    # NB: we deliberately keep BAD common/listed rather than also making it
    # non-common, unlike the most literal reading of the review comment that
    # requested this test. A non-common BAD would correctly report
    # "share_type" (share_type precedes min_history_days in the priority
    # chain -- see test_share_type_reason_wins_over_min_history_days_when_both_apply
    # above), which would make an assertion of "min_history_days" wrong, not
    # a stronger test. This fixture isolates the three checks that actually
    # sit in that relative order (min_history_days -> min_close_usd ->
    # min_adv_usd) instead.
    spec = {"BAD": (4.99, 100, 2), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "BAD") == "min_history_days"


def test_min_close_usd_reason_wins_over_min_adv_usd_when_both_apply():
    # PENNY2 has ample history but is both sub-$5 AND illiquid. min_close_usd
    # is earlier in the chain, so it must win over min_adv_usd.
    spec = {"PENNY2": (4.99, 100, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    assert _reason(u, "PENNY2") == "min_close_usd"


def test_delisting_effective_exactly_on_the_effective_date_is_not_yet_not_listed():
    # SCD-Type-2 reading: effective_to marks the last date the row was
    # effective, so a security whose effective_to equals effective_date is
    # still listed *on* that date -- not_listed requires effective_to
    # strictly before effective_date. This locks in that interpretation
    # (the brief's skeleton and this implementation both use strict `<`)
    # rather than changing behavior.
    spec = {"EDGE": (50.0, 1e6, 5), "OK1": (50.0, 1e6, 5), "OK2": (60.0, 1e6, 5)}
    u = build_universe(
        _px(spec), _master(spec, effective_to_override={"EDGE": EFF}), CAL, CFG, EFF
    )
    assert _reason(u, "EDGE") != "not_listed"


def test_max_names_tie_is_broken_by_security_id_ascending_deterministically():
    # All four securities have identical ADV (same close, same volume), so
    # the max_names=2 cut has no liquidity signal to break the tie on. The
    # documented tiebreaker is security_id ascending: S0 and S1 win.
    spec = {f"S{i}": (50.0, 1e6, 5) for i in range(4)}
    u1 = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    u2 = build_universe(_px(spec), _master(spec), CAL, CFG, EFF)
    included1 = sorted(u1.filter(pl.col("included"))["security_id"].to_list())
    included2 = sorted(u2.filter(pl.col("included"))["security_id"].to_list())
    assert included1 == included2 == ["S0", "S1"]
