"""Economic and persistence regressions for the unified research runtime."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from trading_agent.contracts import AgentError, Bar, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.service import ResearchAgent


def market(count=360, *, cycle=True):
    bars, states = [], []
    day, close = date(2024, 1, 2), D(10000)
    while len(bars) < count:
        if day.weekday() < 5:
            opening = close
            close = (
                (opening * (D("1.02") if len(bars) % 20 < 10 else D(".98"))).quantize(
                    D(1)
                )
                if cycle
                else close
            )
            b = Bar(
                day,
                "FIXTURE",
                opening,
                max(opening, close),
                min(opening, close),
                close,
                100000,
                session_time(day, 15, 40),
                "ENGINEERED_SOFTWARE_FIXTURE",
            )
            bars.append(b)
            states.append(
                SecurityState(
                    "FIXTURE",
                    session_time(day, 15, 30),
                    session_time(day, 15, 40),
                    "NORMAL",
                    "STOCK",
                    "KOSPI",
                    False,
                    False,
                    False,
                    False,
                    close * D(".9995"),
                    close * D("1.0005"),
                    "ENGINEERED_SOFTWARE_FIXTURE",
                )
            )
        day += timedelta(days=1)
    return ResearchInput(Snapshot(bars), SecurityHistory(states))


def policy():
    return AgentPolicy(
        logic=LogicSettings(
            champion=RankedStrategy(20), lookbacks=(2, 20), rankings=("momentum",)
        ),
        holdout_sessions=80,
    )


def staged(data, p):
    end = p.warmup_sessions + p.development_sessions - 1
    return replace(data.prefix(data.sessions[end]), calendar=data.sessions)


def test_restore_rehearsal(tmp_path):
    from momentum.agent.cli import parser, run

    data, p = market(420), policy()
    now = lambda: session_time(data.sessions[-1], 17)
    path = tmp_path / "research.sqlite3"
    store = Store(path)
    agent = ResearchAgent(store, clock=now)
    agent.init("a", p)
    agent.tick("a", data.prefix(data.sessions[285]))
    backup = agent.backup(tmp_path / "backups")
    agent.tick(
        "a",
        replace(data.prefix(data.sessions[285]), calendar=data.sessions),
        max_cycles=2,
    )
    assert store.db.execute(
        "SELECT 1 FROM agent_commitments WHERE ordinal=1"
    ).fetchone()
    store.close()
    run(
        parser().parse_args(
            ["restore", "--from", backup["path"], "--db", str(path), "--replace"]
        )
    )
    store = Store(path)
    agent = ResearchAgent(store, clock=now)
    with pytest.raises(AgentError, match="Witnessed consumption is missing"):
        agent.tick("a", data, max_cycles=2)
    agent.reconcile_witness(operator="fixture")
    assert agent.witness_status()["missing"] == []
    assert (
        store.db.execute("SELECT count(*) FROM agent_witness_quarantine").fetchone()[0]
        >= 2
    )
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_budget_entries WHERE kind='consume'"
        ).fetchone()[0]
        == 2
    )
    agent.reconcile_witness(operator="fixture")
    with pytest.raises(AgentError, match="overlaps|budget"):
        agent.tick("a", data, max_cycles=2)
    assert not store.db.execute(
        "SELECT 1 FROM agent_commitments WHERE ordinal=1"
    ).fetchone()
    store.close()


def test_witness_integrity(tmp_path):
    from momentum.agent.witness import WitnessLog

    path = tmp_path / "witness.jsonl"
    log = WitnessLog(path)
    log.append(
        "interval_claim",
        {
            "market": "KRX",
            "start": "2024-01-01",
            "end": "2024-01-02",
            "owner": "x",
            "kind": "holdout",
        },
    )
    log.append(
        "commitment",
        {
            "commitment_id": "x",
            "study": "a",
            "ordinal": 0,
            "market": "KRX",
            "holdout_start": "2024-01-01",
            "holdout_end": "2024-01-02",
        },
    )
    rows = log.read()
    assert [r["seq"] for r in rows] == [1, 2]
    assert rows[0]["prev_hash"] == "genesis"
    assert rows[1]["prev_hash"] == rows[0]["hash"]
    original = path.read_bytes()
    path.write_bytes(original[:-1])
    with pytest.raises(AgentError, match="Witness log is corrupted"):
        log.read()
    path.write_bytes(original.replace(b'"KRX"', b'"USA"', 1))
    with pytest.raises(AgentError, match="Witness log is corrupted"):
        log.read()


def test_rollback_witness_and_memory(tmp_path):
    data, p = market(), policy()
    store = Store(tmp_path / "rollback.sqlite3")
    agent = ResearchAgent(store)
    with pytest.raises(RuntimeError), store.transaction():
        agent._claim(p, data.sessions[0], data.sessions[1], "lost", "holdout", data)
        raise RuntimeError("commit failed")
    assert agent.witness_status()["missing"]
    agent.reconcile_witness(operator="fixture")
    assert agent.witness_status()["missing"] == []
    store.close()
    memory = Store(":memory:")
    assert ResearchAgent(memory).witness is None
    memory.close()


def test_backup_restore_guards(tmp_path):
    import hashlib
    import json

    from momentum.agent.cli import parser, run

    data, p = market(), policy()
    path = tmp_path / "source.sqlite3"
    store = Store(path)
    agent = ResearchAgent(store, clock=lambda: session_time(data.sessions[-1], 17))
    agent.init("a", p)
    agent.tick("a", data)
    assert agent.witness_status()["missing"] == []
    backup = agent.backup(tmp_path / "backups")
    copy = Path(backup["path"])
    manifest = json.loads(copy.with_suffix(".manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest()
    assert manifest["table_counts"]["agent_commitments"] == 1
    with pytest.raises(AgentError):
        agent.backup(tmp_path / "backups")
    with store.transaction():
        store.db.execute(
            "INSERT INTO agent_intervals VALUES('KRX','2090-01-01','2090-01-02','later','holdout')"
        )
    store.close()
    args = ["restore", "--from", str(copy), "--db", str(path)]
    with pytest.raises(AgentError):
        run(parser().parse_args(args))
    run(parser().parse_args(args + ["--replace"]))
    assert list(tmp_path.glob("source.sqlite3.replaced-*"))
    copy.write_bytes(copy.read_bytes() + b"tampered")
    with pytest.raises(AgentError, match="hash"):
        run(parser().parse_args(args + ["--replace"]))


@pytest.mark.parametrize("field,value", [("seq", 3), ("prev_hash", "wrong")])
def test_witness_chain_rejects_rehashed_record(tmp_path, field, value):
    import json

    from trading_agent.contracts import canonical, digest

    from momentum.agent.witness import WitnessLog

    path = tmp_path / "chain.jsonl"
    log = WitnessLog(path)
    log.append(
        "interval_claim",
        {
            "market": "KRX",
            "start": "2024-01-01",
            "end": "2024-01-02",
            "owner": "a",
            "kind": "shadow",
        },
    )
    row = log.read()[0]
    row[field] = value
    row["hash"] = digest({k: v for k, v in row.items() if k != "hash"})
    path.write_text(canonical(row) + "\n")
    assert json.loads(path.read_text())[field] == value
    with pytest.raises(AgentError, match="Witness log is corrupted"):
        log.read()


def test_observation_extensions_reconcile_atomically(tmp_path):
    data, p = market(), policy()
    store = Store(tmp_path / "observation.sqlite3")
    agent = ResearchAgent(store)
    for end in (1, 3):
        with pytest.raises(RuntimeError), store.transaction():
            agent._claim(
                p,
                data.sessions[0],
                data.sessions[end],
                "stream",
                "observation_exposure",
                data,
            )
            raise RuntimeError("rollback")
    assert len(agent.witness_status()["missing"]) == 2
    agent.reconcile_witness(operator="fixture")
    row = store.db.execute("SELECT * FROM agent_intervals").fetchone()
    assert (row["owner"], row["start"], row["end"]) == (
        "stream",
        data.sessions[0].isoformat(),
        data.sessions[3].isoformat(),
    )
    agent.reconcile_witness(operator="fixture")
    assert (
        store.db.execute("SELECT count(*) FROM agent_witness_quarantine").fetchone()[0]
        == 2
    )
    store.close()


def test_reconcile_rolls_back_all_records_on_failure(tmp_path, monkeypatch):
    data, p = market(), policy()
    store = Store(tmp_path / "atomic.sqlite3")
    agent = ResearchAgent(store)
    with pytest.raises(RuntimeError), store.transaction():
        agent._claim(p, data.sessions[0], data.sessions[1], "lost", "holdout", data)
        agent.witness.append(
            "commitment",
            {
                "commitment_id": "lost",
                "study": "missing",
                "ordinal": 0,
                "market": "KRX",
                "holdout_start": data.sessions[0],
                "holdout_end": data.sessions[1],
            },
        )
        raise RuntimeError("rollback")
    with pytest.raises(AgentError):
        agent.reconcile_witness(operator="fixture")
    assert (
        store.db.execute("SELECT count(*) FROM agent_witness_quarantine").fetchone()[0]
        == 0
    )
    assert store.db.execute("SELECT count(*) FROM agent_intervals").fetchone()[0] == 0
    assert len(agent.witness_status()["missing"]) == 2
    store.close()


def test_witness_status_cli_prints_missing(tmp_path, monkeypatch, capsys):
    import json

    from momentum.agent.cli import main

    path = tmp_path / "cli.sqlite3"
    data, p = market(), policy()
    store = Store(path)
    agent = ResearchAgent(store)
    with pytest.raises(RuntimeError), store.transaction():
        agent._claim(p, data.sessions[0], data.sessions[1], "lost", "holdout", data)
        raise RuntimeError("rollback")
    store.close()
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent",
            "witness-status",
            "--db",
            str(path),
            "--output",
            str(tmp_path / "reports"),
        ],
    )
    assert main() == 0
    assert len(json.loads(capsys.readouterr().out)["missing"]) == 1


def test_init_is_blocked_by_missing_witness(tmp_path):
    data, p = market(), policy()
    store = Store(tmp_path / "init.sqlite3")
    agent = ResearchAgent(store)
    with pytest.raises(RuntimeError), store.transaction():
        agent._claim(p, data.sessions[0], data.sessions[1], "lost", "holdout", data)
        raise RuntimeError("rollback")
    with pytest.raises(AgentError, match="Witnessed consumption is missing"):
        agent.init("new", p)
    assert not store.db.execute("SELECT 1 FROM agent_studies").fetchone()
    store.close()


def test_backup_snapshot_counts_and_wal_preservation(tmp_path):
    import json
    import sqlite3

    from momentum.agent.cli import parser, run

    path = tmp_path / "wal.sqlite3"
    store = Store(path)
    agent = ResearchAgent(store)
    agent.init("a", policy())
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert Path(str(path) + "-wal").exists()
    backup = agent.backup(tmp_path / "copies")
    manifest = json.loads(Path(backup["manifest"]).read_text())
    copy = sqlite3.connect(backup["path"])
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, count in manifest["table_counts"].items():
            assert (
                copy.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == count
            )
    finally:
        copy.close()
    with store.transaction():
        store.db.execute(
            "INSERT INTO agent_intervals VALUES('KRX','2091-01-01','2091-01-02','later','shadow')"
        )
    store.close()
    # Preserve sidecars byte-for-byte, with names usable by the preserved DB.
    wal, shm = Path(str(path) + "-wal"), Path(str(path) + "-shm")
    wal.write_bytes(b"preserved-wal")
    shm.write_bytes(b"preserved-shm")
    run(
        parser().parse_args(
            ["restore", "--from", backup["path"], "--db", str(path), "--replace"]
        )
    )
    preserved = list(tmp_path.glob("wal.sqlite3.replaced-*"))
    assert any(
        p.read_bytes() == b"preserved-wal" for p in preserved if p.name.endswith("-wal")
    )
    assert any(
        p.read_bytes() == b"preserved-shm" for p in preserved if p.name.endswith("-shm")
    )
