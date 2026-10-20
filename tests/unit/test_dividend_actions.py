"""Cash-dividend actions: ex-date, policy A payment date, and refusals."""

import importlib.util
import json
import sys
from datetime import date
from decimal import Decimal as D
from pathlib import Path

from momentum.agent.contracts import AnnouncedAction
from momentum.krx.dividend_actions import add_month, build
from momentum.ledger.ledger import CorporateAction

SESSIONS = (
    date(2023, 12, 26),
    date(2023, 12, 27),
    date(2023, 12, 28),
    date(2024, 1, 2),
)
CONFIRMED = date(2024, 1, 2)


def record(
    rcept_no="20231215000001",
    *,
    available="2023-12-15T18:00:00+09:00",
    amount="5000",
    pay=None,
    agm="2024-03-27",
    board="2023-12-15",
    **extra,
):
    return {
        "rcept_no": rcept_no,
        "stock_code": "033780",
        "corp_name": "KT&G",
        "available_at": available,
        "dividend_kind": "결산배당",
        "payment_kind": "현금배당",
        "per_share_common": amount,
        "per_share_class": None,
        "record_date": "2023-12-31",
        "pay_date": pay,
        "agm_date": agm,
        "board_date": board,
        "subsidiary": None,
        "correction": False,
        **extra,
    }


def test_add_month_clamps_to_month_end():
    assert add_month(date(2024, 1, 31)) == date(2024, 2, 29)
    assert add_month(date(2024, 12, 15)) == date(2025, 1, 15)


def test_policy_a_payment_dates_and_ex_date():
    stated = build([record(pay="2024-04-19")], SESSIONS, CONFIRMED).actions[0]
    assert (stated["ex_date"], stated["pay_date"]) == ("2023-12-27", "2024-04-19")
    assert stated["evidence"]["pay_date_basis"] == "stated"
    via_agm = build([record()], SESSIONS, CONFIRMED).actions[0]
    assert (
        via_agm["pay_date"] == "2024-04-27"
        and via_agm["evidence"]["pay_date_basis"] == "agm+1m"
    )
    via_board = build(
        [record(agm=None, board="2024-02-07")], SESSIONS, CONFIRMED
    ).actions[0]
    assert via_board["pay_date"] == "2024-03-07"
    assert via_board["evidence"]["pay_date_basis"] == "board+1m"
    none = build([record(agm=None, board=None)], SESSIONS, CONFIRMED)
    assert (
        none.actions == []
        and none.unresolved[0]["reason"] == "no payment date, AGM or board date"
    )


def test_action_loads_as_announced_cash_dividend():
    action = build([record()], SESSIONS, CONFIRMED).actions[0]
    announced = AnnouncedAction(
        CorporateAction(
            action["security_id"],
            action["action_type"],
            date.fromisoformat(action["ex_date"]),
            date.fromisoformat(action["pay_date"]),
            None,
            D(action["cash_amount"]),
        ),
        datetime_from(action["available_at"]),
    )
    assert announced.action.cash_amount == D(5000)


def datetime_from(text):
    from datetime import datetime

    return datetime.fromisoformat(text)


def test_corrections_late_filings_and_other_refusals():
    original = record()
    corrected = record(
        "20231220000002",
        available="2023-12-20T18:00:00+09:00",
        amount="5000",
        correction=True,
        pay="2024-04-19",
    )
    result = build([corrected, original], SESSIONS, CONFIRMED)
    (action,) = result.actions
    assert action["evidence"]["rcept_no"] == "20231220000002"
    assert action["evidence"]["filings"] == ["20231215000001", "20231220000002"]
    changed_late = record(
        "20240105000003", available="2024-01-05T18:00:00+09:00", amount="5100"
    )
    late = build([original, changed_late], SESSIONS, CONFIRMED)
    assert (
        late.actions == []
        and late.unresolved[0]["reason"] == "amount changed after the ex-date open"
    )
    # Policy A (2026-10-20): a dividend published after its ex-date is booked late.
    only_late = build(
        [record(available="2023-12-28T18:00:00+09:00")], SESSIONS, CONFIRMED
    )
    (late_action,) = only_late.actions
    assert late_action["action_type"] == "late_cash_dividend"
    assert late_action["recognized_on"] == "2024-01-02"
    others = build(
        [
            record(subsidiary="SK텔레콤(주)"),
            record(payment_kind="현물배당"),
            record(amount=None),
        ],
        SESSIONS,
        CONFIRMED,
    )
    reasons = sorted(u["reason"].split(":")[0] for u in others.unresolved)
    assert reasons == [
        "no common-share amount",
        "non-cash payment",
        "subsidiary filing; payer has no stock code",
    ]
    unconfirmed = build([record()], SESSIONS, date(2023, 12, 28))
    assert "not confirmed" in unconfirmed.unresolved[0]["reason"]


def test_payment_not_after_ex_date_is_refused():
    result = build([record(pay="2023-12-27")], SESSIONS, CONFIRMED)
    assert (
        result.actions == [] and "not after ex-date" in result.unresolved[0]["reason"]
    )


def test_cli_round_trip(tmp_path, capsys):
    dividends = tmp_path / "dividends.jsonl"
    dividends.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    calendar = tmp_path / "calendar"
    calendar.mkdir()
    (calendar / "calendar.json").write_text(
        json.dumps([d.isoformat() for d in SESSIONS])
    )
    (calendar / "calendar-report.json").write_text(
        json.dumps({"confirmed_through": "2024-01-02"})
    )
    spec = importlib.util.spec_from_file_location(
        "dividend_actions_tool",
        Path(__file__).resolve().parents[2] / "tools/dividend_actions.py",
    )
    assert spec and spec.loader
    tool = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = tool
    spec.loader.exec_module(tool)
    out = tmp_path / "out"
    assert (
        tool.main(
            [
                "--dividends",
                str(dividends),
                "--calendar",
                str(calendar),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert (
        capsys.readouterr().out.strip() == "dividend-actions: 1 actions, 0 unresolved"
    )
    assert json.loads((out / "actions.json").read_text())[0]["cash_amount"] == "5000"


def test_deferred_record_date_produces_no_action():
    result = build([record(record_date=None)], SESSIONS, CONFIRMED)
    assert result.actions == []
    assert result.unresolved[0]["reason"] == "record date set by a separate filing"
