"""tools/holdings_audit.py on a synthetic study database (no real data)."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("holdings_audit", ROOT / "tools/holdings_audit.py")
ha = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = ha
spec.loader.exec_module(ha)

JUMPS = {"221610": [{"symbol": "221610", "last_day": "2021-04-28", "day": "2021-05-21", "close_ratio": 43.2}]}


def curve(rows):
    return [{"session": s, "equity": "1", "positions": p} for s, p in rows]


def test_a_name_held_into_a_jump_day_is_an_exposure_and_one_sold_before_is_not():
    held = curve([("2021-04-27", {"221610": 100}), ("2021-04-28", {"221610": 100}), ("2021-05-21", {})])
    sold = curve([("2021-04-27", {"221610": 100}), ("2021-04-28", {}), ("2021-05-21", {})])
    later = curve([("2021-06-01", {"221610": 100})])                       # starts after the jump
    evidence = {"holdout": {"baseline": {"equity_curve": held}, "candidate": {"equity_curve": sold}},
                "trials": [{"result": {"equity_curve": later}}]}
    found = ha.scan([("db", "study#3.evidence", evidence)], JUMPS)
    assert [(f["path"], f["symbol"], f["day"]) for f in found] == [("holdout.baseline", "221610", "2021-05-21")]


def test_study_databases_are_read_without_writing(tmp_path):
    db = tmp_path / "research.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE agent_cycles (id TEXT, study TEXT, ordinal INTEGER, status TEXT, manifest_json TEXT,"
                " evidence_json TEXT, evidence_hash TEXT, result_json TEXT)")
    ev = {"holdout": {"baseline": {"equity_curve": curve([("2021-04-28", {"221610": 5}), ("2021-05-21", {})])}}}
    con.execute("INSERT INTO agent_cycles VALUES ('c', 'explore-v6', 4, 'EVALUATED', '{}', ?, 'h', NULL)",
                (json.dumps(ev),))
    con.commit()
    con.close()
    sources = list(ha.from_db(db))
    assert [label for _, label, _ in sources] == ["explore-v6#4.evidence"]
    found = ha.scan(sources, JUMPS)
    assert len(found) == 1 and found[0]["source"] == "explore-v6#4.evidence"
