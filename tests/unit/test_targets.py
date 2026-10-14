import inspect
from decimal import Decimal

from momentum.config import ExecutionCfg
from momentum.portfolio import targets as T

CFG = ExecutionCfg(cash_buffer=0.0)


def test_signature_cannot_see_the_execution_price_at_all():
    params = set(inspect.signature(T.compute_targets).parameters)
    assert not {"open_px", "open_price", "fill_price", "opens"} & params


def test_weight_is_capped_at_five_percent():
    ranked = [f"S{i}" for i in range(10)]
    px = {s: Decimal(100) for s in ranked}
    out = T.compute_targets(ranked, Decimal(30000), px, {}, CFG)
    # 1/N = 10% 이지만 상한 5% → 종목당 $1500 → 15주
    assert all(o.shares == 15 for o in out if o.side == "buy")


def test_fewer_than_twenty_names_leaves_residual_cash():
    ranked = [f"S{i}" for i in range(5)]
    px = {s: Decimal(100) for s in ranked}
    out = T.compute_targets(ranked, Decimal(30000), px, {}, CFG)
    assert sum(o.shares * 100 for o in out) <= 30000 * 0.05 * 5


def test_held_name_is_net_traded_not_round_tripped():
    out = T.compute_targets(
        ["A", "B"],
        Decimal(30000),
        {"A": Decimal(100), "B": Decimal(100)},
        {"A": 10},
        CFG,
    )
    a = [o for o in out if o.security_id == "A"]
    assert len(a) == 1 and a[0].side == "buy" and a[0].shares == 5  # 목표 15, 보유 10


def test_dropped_name_is_fully_sold_and_ordered_before_any_buy():
    out = T.compute_targets(
        ["A"],
        Decimal(30000),
        {"A": Decimal(100), "Z": Decimal(50)},
        {"Z": 20},
        CFG,
    )
    assert out[0] == T.TargetOrder("Z", "sell", 20, -1)
    assert all(o.side == "buy" for o in out[1:])


def test_buys_are_emitted_in_signal_rank_order():
    ranked = ["A", "B", "C"]
    px = {s: Decimal(100) for s in ranked}
    out = [o for o in T.compute_targets(ranked, Decimal(30000), px, {}, CFG) if o.side == "buy"]
    assert [o.security_id for o in out] == ranked


def test_zero_selected_names_returns_no_orders_rather_than_erroring():
    assert T.compute_targets([], Decimal(30000), {}, {}, CFG) == []


def test_stock_too_expensive_for_one_share_is_omitted():
    out = T.compute_targets(["EXP"], Decimal(30000), {"EXP": Decimal(9999)}, {}, CFG)
    assert out == []  # 5% = $1500 < 1주 가격


def test_more_than_fifty_names_are_truncated_to_the_cap():
    ranked = [f"S{i}" for i in range(80)]
    px = {s: Decimal(10) for s in ranked}
    out = T.compute_targets(ranked, Decimal(30000), px, {}, CFG)
    assert len({o.security_id for o in out}) == 50


# --- authorized additions ---


def test_buy_notionals_never_exceed_investable_cash_across_n():
    cfg = ExecutionCfg(cash_buffer=0.005)
    for n in (5, 15, 20, 21, 35, 50, 60):
        ranked = [f"S{i}" for i in range(n)]
        px = {s: Decimal("37.13") for s in ranked}
        nav = Decimal(30000)
        out = T.compute_targets(ranked, nav, px, {}, cfg)
        spent = sum((o.shares * px[o.security_id] for o in out if o.side == "buy"), Decimal(0))
        assert spent <= nav * (Decimal(1) - Decimal(str(cfg.cash_buffer)))


def test_security_missing_from_prev_close_is_skipped_without_shifting_other_weights():
    # N=25 selected, 1 missing from prev_close (24 priced). At this size neither
    # candidate weight hits the 5% cap, so the two denominators are distinguishable:
    #   correct (selected count):  1/25 = 0.04      -> 30000*0.04/113  = 10.619 -> 10 shares
    #   bug (priced-name count):   1/24 = 0.041667   -> 30000/24/113   = 11.061 -> 11 shares
    # A test asserting 15 shares (as the old N=3 version did) would pass under either
    # denominator, since both get capped to 0.05 there — this fixture is chosen so the
    # two readings disagree, and we assert the correct (selected-count) answer.
    ranked = [f"S{i}" for i in range(25)]
    px = {s: Decimal(113) for s in ranked[:-1]}  # last one ("S24") missing from prev_close
    out = T.compute_targets(ranked, Decimal(30000), px, {}, CFG)
    buys = {o.security_id: o.shares for o in out if o.side == "buy"}
    assert "S24" not in buys
    assert all(shares == 10 for shares in buys.values())


def test_held_security_already_at_target_produces_no_order():
    out = T.compute_targets(
        ["A"],
        Decimal(30000),
        {"A": Decimal(100)},
        {"A": 15},
        CFG,
    )
    assert out == []


def test_held_selected_security_missing_price_is_left_unmanaged():
    # A held, selected security whose prev_close has vanished gets neither a buy nor
    # a sell: rule 3 cancels the BUY on missing price data, it does not authorize a
    # forced SELL, and inventing a liquidation the data doesn't support would be worse
    # than leaving the position flat. The other selected names must be unaffected.
    ranked = ["A", "B"]
    px = {"B": Decimal(100)}  # "A" missing from prev_close
    out = T.compute_targets(ranked, Decimal(30000), px, {"A": 7}, CFG)
    assert not any(o.security_id == "A" for o in out)
    b = [o for o in out if o.security_id == "B"]
    assert len(b) == 1 and b[0].side == "buy" and b[0].shares == 15


def test_output_is_deterministic_regardless_of_holdings_dict_order():
    ranked = ["A", "B", "C"]
    px = {s: Decimal(100) for s in ranked}
    holdings1 = {"Z": 5, "Y": 3}
    holdings2 = {"Y": 3, "Z": 5}
    out1 = T.compute_targets(ranked, Decimal(30000), px, holdings1, CFG)
    out2 = T.compute_targets(ranked, Decimal(30000), px, holdings2, CFG)
    assert out1 == out2
