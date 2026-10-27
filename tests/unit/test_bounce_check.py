"""tools/bounce_check.py: last-trade vs mid reversals on fake probe days (no network)."""

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bounce_check", ROOT / "tools/bounce_check.py")
check = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = check
spec.loader.exec_module(check)
KST = check.KST


def write_day(root, day, mids, trades, symbol="069500"):
    """``mids``/``trades``: one value per minute from 09:00 (None = no trade that minute)."""
    out = root / day
    out.mkdir(parents=True)
    start = datetime.fromisoformat(f"{day}T09:00:00").replace(tzinfo=KST)
    with (out / f"asking-{symbol}.jsonl").open("w") as f:
        for k, mid in enumerate(mids):
            for second in (5, 35):                           # two snapshots inside the minute
                at = start + timedelta(minutes=k, seconds=second)
                book = {"askp1": str(mid + 2.5), "bidp1": str(mid - 2.5), "askp_rsqn1": "100", "bidp_rsqn1": "100"}
                f.write(json.dumps({"received_at": at.isoformat(), "elapsed_ms": 50,
                                    "response": {"output1": book}}) + "\n")
    rows = [{"stck_bsop_date": day.replace("-", ""), "stck_cntg_hour": (start + timedelta(minutes=k)).strftime("%H%M%S"),
             "stck_prpr": str(p), "cntg_vol": "10"} for k, p in enumerate(trades) if p is not None]
    (out / f"minute-{symbol}.jsonl").write_text(json.dumps(
        {"pull": "close", "received_at": f"{day}T15:31:00+09:00", "elapsed_ms": 50, "response": {"output2": rows}}) + "\n")
    return out


def test_a_bouncing_trade_price_over_a_still_mid_is_all_reversal(tmp_path):
    n = check.GRID
    mids = [10000.0] * n
    trades = [10002.5 if k % 2 else 9997.5 for k in range(n)]   # bid, ask, bid, ... around a fixed mid
    result = check.check([write_day(tmp_path, "2026-10-28", mids, trades)])
    h1 = result["pooled"]["h1"]
    assert h1["trade"]["reversal"] == 1.0 and h1["trade"]["pairs"] == n - 2
    assert h1["mid"]["pairs"] == 0 and h1["mid"]["reversal"] is None   # the mid never moved
    assert h1["bounce_share"] is None


def test_a_trending_mid_shows_no_reversal_on_either_series(tmp_path):
    n = check.GRID
    mids = [10000.0 + 5 * k for k in range(n)]
    trades = [m + 2.5 for m in mids]
    result = check.check([write_day(tmp_path, "2026-10-28", mids, trades)])
    for h in ("h1", "h5"):
        cell = result["pooled"][h]
        assert cell["trade"]["reversal"] == cell["mid"]["reversal"] == 0.0
    assert result["pooled"]["h5"]["trade"]["pairs"] == n - 10


def test_minutes_without_a_trade_keep_the_last_price_and_nothing_is_invented_before(tmp_path):
    n = check.GRID
    mids = [10000.0 + 5 * k for k in range(n)]
    trades = [None, None] + [m + 2.5 for m in mids[2:]]
    trades[100] = None
    cell = check.check([write_day(tmp_path, "2026-10-28", mids, trades)])["pooled"]["h1"]["trade"]
    assert cell["reversal"] == 0.0
    assert cell["pairs"] == n - 2 - 2 - 2      # two minutes before the first trade, one flat step around 100


def test_bounce_share_and_per_symbol_rows(tmp_path):
    n = check.GRID
    mids = [10000.0 + (0, 5, 10, 5)[k % 4] for k in range(n)]   # mid: up, up, down, down, ...
    trades = [10002.5 if k % 2 else 9997.5 for k in range(n)]
    result = check.check([write_day(tmp_path, "2026-10-28", mids, trades)])
    h1 = result["pooled"]["h1"]
    assert h1["trade"]["reversal"] == 1.0 and h1["mid"]["reversal"] == pytest.approx(0.5, abs=0.01)
    assert h1["bounce_share"] == pytest.approx(1.0, abs=0.02)    # all of the excess over 0.5 is the trade price
    assert set(result["symbols"]) == {"069500"} and result["days"] == ["2026-10-28"]
    assert check.main([str(tmp_path / "2026-10-28")]) == 0
