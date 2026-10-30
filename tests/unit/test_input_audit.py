"""tools/input_audit.py on synthetic research inputs (no real data)."""

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from momentum.ledger.ledger import CorporateAction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/unit"))
import test_ma_cross as t

spec = importlib.util.spec_from_file_location("input_audit", ROOT / "tools/input_audit.py")
audit = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def test_a_merger_across_a_suspension_is_found_and_a_recorded_split_is_not():
    n = 40
    sessions = t.weekdays(n)
    merged = [330.0] * 20 + [15450.0] * 20                          # x46.8 across a gap, nothing recorded
    split = [5000.0] * 25 + [1000.0] * 15                            # 1:5, recorded
    data, _ = t.make_data({"221610": merged, "005930": split, "000660": [100.0 + k for k in range(n)]}, n=n)
    data.snapshot.bars = [b for b in data.snapshot.bars
                          if not (b.symbol == "221610" and 14 <= sessions.index(b.session) < 20)]
    data.actions = (SimpleNamespace(action=CorporateAction("005930", "split", sessions[25], None, Decimal(5), None),
                                    available_at=datetime.combine(sessions[20], datetime.min.time(), t.KST)),)
    result = audit.audit(data)
    assert [(f["symbol"], f["last_day"], f["day"], f["gap_sessions"]) for f in result["jumps"]] == [
        ("221610", sessions[13].isoformat(), sessions[20].isoformat(), 6)]
    assert round(result["jumps"][0]["close_ratio"], 3) == round(15450 / 330, 3)
    assert result["symbols_with_jumps"] == 1 and result["after_a_gap"] == 1
    assert "221610" in audit.text(result)


def test_a_limit_up_day_is_not_a_jump():
    data, _ = t.make_data({"005930": [100.0] * 10 + [129.0] * 10}, n=20)
    assert audit.audit(data)["jumps"] == []
