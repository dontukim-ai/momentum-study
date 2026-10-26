"""tools/fwd_record.py: forward-record-v9 targets end to end on a synthetic input."""

import importlib.util
import json
import math
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from momentum.agent.cli import read_input
from momentum.agent.contracts import policy_from_dict
from momentum.agent.market import KRX
from momentum.config import SpecUndefinedError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fwd_record_tool", ROOT / "tools/fwd_record.py")
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = tool  # dataclasses look their module up
spec.loader.exec_module(tool)

FIXTURES = ROOT / "tests/fixtures/forward-v9"
STOCKS = [f"{100000 + i * 10:06d}" for i in range(12)]
ETF = "069500"


def base_policy():
    return policy_from_dict(json.loads(tool.BASE.read_text(encoding="utf-8")))


def write_input(root: Path, count: int = 270) -> list[date]:
    """Twelve liquid stocks and the market ETF, whole-won prices, no actions."""
    sessions, day = [], date(2025, 9, 1)
    while len(sessions) < count:
        if day.weekday() < 5:
            sessions.append(day)
        day += timedelta(days=1)
    root.mkdir(parents=True)
    rows = ["session,symbol,open,high,low,close,volume,available_at,source"]
    for i, symbol in enumerate([*STOCKS, ETF]):
        close = D(40000 + 1000 * i)
        for t, session in enumerate(sessions):
            drift = 0.0004 * (i - 6) if symbol != ETF else 0.0003
            change = drift + 0.012 * math.sin(t / (5 + i) + i)
            opening = close
            close = (opening * D(str(round(1 + change, 6)))).quantize(D(1))
            rows.append(
                f"{session},{symbol},{opening},{max(opening, close)},{min(opening, close)},{close},"
                f"{500000 + 1000 * i},{KRX.historical_bar_at(session).isoformat()},SYNTHETIC"
            )
    (root / "prices.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    first = sessions[0]
    records = [
        {"symbol": s, "effective_at": KRX.open_at(first).isoformat(),
         "available_at": KRX.observed_at(first).isoformat(), "status": "NORMAL",
         "asset_type": "ETF" if s == ETF else "STOCK", "market": "KOSPI", "managed": False,
         "caution": False, "leveraged": False, "inverse": False, "bid": None, "ask": None,
         "source": "SYNTHETIC"}
        for s in [*STOCKS, ETF]
    ]
    (root / "securities.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    (root / "actions.json").write_text("[]", encoding="utf-8")
    (root / "sessions.json").write_text(json.dumps([d.isoformat() for d in sessions]), encoding="utf-8")
    filed = KRX.observed_at(first).isoformat()
    # Reports older than 15 months drop out, so the window needs the 2025 one.
    (root / "financials.jsonl").write_text("".join(
        json.dumps({"symbol": s, "period_end": end, "available_at": at, "statement": "CFS",
                    "equity": str(10**12 * (1 + i % 5)), "assets": str(3 * 10**12),
                    "operating_income": str(10**11 * (1 + i % 3)), "source": "SYNTHETIC"}) + "\n"
        for end, at in (("2024-12-31", filed), ("2025-12-31", KRX.observed_at(date(2026, 3, 20)).isoformat()))
        for i, s in enumerate(STOCKS)), encoding="utf-8")
    (root / "shares.csv").write_text(
        "symbol,session,through,available_at,listed_shares,source\n" + "".join(
            f"{s},{first},{sessions[-1]},{filed},{10**7 * (i + 1)},SYNTHETIC\n" for i, s in enumerate(STOCKS)),
        encoding="utf-8")
    return sessions


def load(root: Path):
    return read_input(
        root / "prices.csv", root / "securities.jsonl", root / "actions.json", root / "sessions.json",
        securities_mode="session-confirmed", financials=root / "financials.jsonl", shares=root / "shares.csv",
    )


def test_derived_policies_are_the_study_policies():
    derived = tool.policies(base_policy())
    for name, fixture in (("v7", "explore-v7-value-A"), ("c1", "c1-trend"), ("c3", "c3-residual")):
        assert derived[name] == policy_from_dict(json.loads((FIXTURES / f"{fixture}.json").read_text())), name
    # C4 and the 95% ETF only change what explore v8 §7 fixed.
    v7, c4, etf = derived["v7"], derived["c4"], derived["etf95"]
    assert c4.logic.selection == replace(
        v7.logic.selection, max_market_cap_rank=10, max_gross=D("0.95"), max_order_notional=D(100000000))
    assert c4.decision_cadence == "monthly" and replace(c4, logic=v7.logic) == v7
    assert etf.logic.selection == replace(
        v7.logic.selection, max_position=D("0.95"), max_gross=D("0.95"), max_order_notional=D(100000000))


def test_targets_are_the_nine_fixed_targets():
    names = [(t.name, None if t.spec is None else (t.spec.ranking, t.spec.lookback, t.spec.top_n, t.spec.gross_weight))
             for t in tool.targets(base_policy())]
    assert names == [
        ("R1", None), ("R2", None),
        ("S1", ("momentum", 20, 10, D("0.8"))), ("S2", ("momentum", 20, 10, D("0.8"))),
        ("S3", ("value", 20, 10, D("0.8"))), ("S4", ("value_profit", 20, 10, D("0.8"))),
        ("S5", ("market_trend", 20, 1, D("0.4"))), ("S6", ("residual_momentum", 60, 10, D("0.8"))),
        ("S7", ("market_cap_trend", 20, 10, D("0.95"))),
    ]


def test_base_must_be_the_v6_policy():
    p = base_policy()
    with pytest.raises(SpecUndefinedError, match="v6"):
        tool.policies(replace(p, logic=replace(p.logic, selection=replace(p.logic.selection, max_market_cap_rank=200))))


def test_record_runs_every_target_from_the_start(tmp_path):
    sessions = write_input(tmp_path / "in")
    data, base = load(tmp_path / "in"), base_policy()
    start, end = sessions[230], sessions[-1]
    result = tool.record(data, base, start, None)
    assert (result["start"], result["end"], result["sessions"]) == (start.isoformat(), end.isoformat(), 40)
    rows = result["targets"]
    assert list(rows) == ["R1", "R2", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert all(math.isfinite(r["net_return"]) and 0 <= r["max_drawdown"] < 1 for r in rows.values())
    assert all(rows[n]["trades"] > 0 for n in ("R1", "R2", "S1", "S2", "S3", "S4", "S6", "S7"))
    assert all(("a_return_ge_r2" in r) == n.startswith("S") for n, r in rows.items())
    # A shorter end evaluates only the prefix; the same call is reproducible.
    assert tool.record(data, base, start, sessions[250])["sessions"] == 21
    assert tool.record(load(tmp_path / "in"), base, start, None) == result
    with pytest.raises(SpecUndefinedError, match="no session"):
        tool.record(data, base, end + timedelta(days=7))


def test_cli_prints_the_table_and_one_json_line(tmp_path, capsys):
    sessions = write_input(tmp_path / "in")
    root = tmp_path / "in"
    assert tool.main(["--input", str(root), "--financials", str(root / "financials.jsonl"),
                      "--shares", str(root / "shares.csv"), "--start", sessions[230].isoformat()]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(f"forward record {sessions[230]}~{sessions[-1]} (40 sessions)")
    assert json.loads(lines[-1])["targets"]["S7"]["label"].startswith("C4")
