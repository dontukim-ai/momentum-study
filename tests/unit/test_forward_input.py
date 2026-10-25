"""tools/forward_input.py: every holdout starts on or after the chosen date."""

import importlib.util
import json
from pathlib import Path

import pytest
from test_eod_observation import historical, historical_policy
from trading_agent.contracts import canonical
from trading_agent.data import write_csv
from trading_agent.selection import write_security_history
from trading_agent.store import Store

from momentum.agent.cli import read_input
from momentum.agent.market import KRX
from momentum.agent.service import ResearchAgent

spec = importlib.util.spec_from_file_location(
    "forward_input_tool", Path(__file__).resolve().parents[2] / "tools/forward_input.py"
)
tool = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tool)


def dividend(known, day, pay):
    return {
        "security_id": "HIST",
        "action_type": "cash_dividend",
        "ex_date": day.isoformat(),
        "pay_date": pay.isoformat(),
        "split_ratio": None,
        "cash_amount": "10",
        "available_at": KRX.observed_at(known).isoformat(),
    }


def write_input(root, data, actions):
    root.mkdir()
    write_csv(data.snapshot, root / "prices.csv")
    write_security_history(data.securities, root / "securities.jsonl")
    (root / "actions.json").write_text(json.dumps(actions))
    (root / "sessions.json").write_text(
        json.dumps([d.isoformat() for d in data.sessions])
    )


def test_first_holdout_lands_on_the_chosen_session(tmp_path):
    data = historical(320)
    sessions = data.sessions
    first = sessions[200]
    source, out = tmp_path / "full", tmp_path / "fwd"
    write_input(source, data, [
            dividend(sessions[2], sessions[5], sessions[30]),
            dividend(sessions[245], sessions[250], sessions[270]),
        ])
    policy = historical_policy()
    (tmp_path / "policy.json").write_text(canonical(policy))
    report = tool.forward_input(source, tmp_path / "policy.json", first, out)
    offset = policy.warmup_sessions + policy.development_sessions + policy.embargo_sessions
    assert report["first_kept_session"] == sessions[200 - offset].isoformat()
    assert report["first_holdout_session"] == first.isoformat()
    assert report["actions"] == [2, 1]
    assert report["price_rows"] == [320, 320 - (200 - offset)]
    # The security record set before the cut is kept and still applies.
    assert (out / "securities.jsonl").read_text() == (source / "securities.jsonl").read_text()

    cut = read_input(
        out / "prices.csv",
        out / "securities.jsonl",
        out / "actions.json",
        out / "sessions.json",
        securities_mode="session-confirmed",
    )
    store = Store(tmp_path / "agent.sqlite3")
    agent = ResearchAgent(store)
    agent.init("fwd", policy)
    agent.tick("fwd", cut)
    payload = json.loads(
        store.db.execute(
            "SELECT payload_json FROM agent_commitments ORDER BY ordinal LIMIT 1"
        ).fetchone()[0]
    )
    assert payload["holdout_start"] == first.isoformat()
    store.close()


def test_too_little_history_before_the_first_holdout_is_refused(tmp_path):
    data = historical(230)
    write_input(tmp_path / "full", data, [])
    (tmp_path / "policy.json").write_text(canonical(historical_policy()))
    with pytest.raises(tool.SpecUndefinedError, match="are needed"):
        tool.forward_input(
            tmp_path / "full", tmp_path / "policy.json", data.sessions[100], tmp_path / "o"
        )
