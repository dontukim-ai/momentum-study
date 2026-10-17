"""Cohort reservation, exposure isolation and continuous synthetic E2 regressions."""

import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest
from trading_agent.contracts import AgentError, Bar, digest, session_time
from trading_agent.data import Snapshot
from trading_agent.logic import LogicSettings
from trading_agent.selection import RankedStrategy, SecurityHistory, SecurityState
from trading_agent.store import Store

from momentum.agent.contracts import AgentPolicy, ResearchInput
from momentum.agent.service import FAMILY_LINEAGE_ID, ResearchAgent


def market(count=360, *, cycle=True, start=date(2024, 1, 2)):
    bars, states = [], []
    day, close = start, D(10000)
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


def prefix(data, index):
    return replace(data.prefix(data.sessions[index]), calendar=data.calendar)


def stream():
    return f"observation:KRX:{FAMILY_LINEAGE_ID}"


def setup_agent(tmp_path, data, p=None):
    store = Store(tmp_path / "cohort.sqlite3")
    now = [session_time(data.sessions[-1], 17)]
    agent = ResearchAgent(store, clock=lambda: now[0])
    agent.init("a", p or policy())
    return store, agent, now


def cohort_rows(store):
    return [
        dict(r) for r in store.db.execute("SELECT * FROM agent_cohorts ORDER BY start")
    ]


def test_e2_continuous_observation_and_sealed_privacy(tmp_path):
    data = market(480)
    store, agent, now = setup_agent(tmp_path, data)
    # Complete the first synthetic promotion before cycle one's development end.
    now[0] = session_time(data.sessions[281], 17)
    first = agent.tick("a", prefix(data, 281))
    assert first["cycles"][0]["research_champion_updated"]
    previous = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    plan = agent._plan(data, policy(), previous)
    now[0] = session_time(data.sessions[plan["dev_end"]], 17)
    report = agent.tick("a", prefix(data, plan["dev_end"]), max_cycles=2)
    cohorts = cohort_rows(store)
    second = cohorts[1]
    assert second["registered_before_start"] == 1
    assert json.loads(second["observation_streams_json"]) == [stream()]
    identity = {
        k: second[k]
        for k in ("market", "start", "end", "commitment_id", "baseline_version")
    }
    identity["observation_streams"] = [stream()]
    assert second["cohort_id"] == digest(identity)
    assert report["observation"]["end"] == data.sessions[plan["dev_end"]].isoformat()
    assert report["observation"]["exposure_recorded"] is True
    before = store.db.execute(
        "SELECT end FROM agent_intervals WHERE kind='observation_exposure'"
    ).fetchone()[0]
    middle = plan["holdout_start"] + 10
    now[0] = session_time(data.sessions[middle], 17)
    report = agent.tick("a", prefix(data, middle), max_cycles=2)
    observation = report["observation"]
    assert observation["end"] == data.sessions[middle].isoformat()
    assert observation["exposure_recorded"] is True
    assert observation["sealed"] is True
    assert observation["proposer_visible"] is False
    assert (
        store.db.execute(
            "SELECT end FROM agent_intervals WHERE kind='observation_exposure'"
        ).fetchone()[0]
        > before
    )
    assert agent.status("a")["cohorts"][1]["sealed"] is True
    assert all(m["source_phase"] != "observation" for m in agent.development_memory())
    now[0] = session_time(data.sessions[plan["holdout_end"]], 17)
    report = agent.tick("a", prefix(data, plan["holdout_end"]), max_cycles=2)
    assert report["cycles"][1]["evidence_grade"] == "E2"
    assert report["status"]["cycles"][1]["status"] == "COMPLETE"
    assert report["status"]["cohorts"][1]["sealed"] is False
    assert report["observation"]["sealed"] is False
    assert report["observation"]["exposure_recorded"] is True
    assert cohort_rows(store) == cohorts
    # The next commitment can use released research memory, never this ledger.
    now[0] = session_time(data.sessions[-1], 17)
    agent.tick("a", data, max_cycles=3)
    payload = json.loads(
        store.db.execute(
            "SELECT payload_json FROM agent_commitments WHERE ordinal=2"
        ).fetchone()[0]
    )
    phases = {
        r["snapshot_id"]: r["source_phase"]
        for r in store.db.execute("SELECT * FROM agent_failure_snapshots")
    }
    assert all(phases[s] != "observation" for s in payload["proposer_inputs"])
    store.close()


def test_observation_stops_before_next_uncommitted_holdout(tmp_path):
    data = market(480)
    store, agent, _ = setup_agent(tmp_path, data)
    report = agent.tick("a", data)
    previous = store.db.execute("SELECT * FROM agent_cycles").fetchone()
    plan = agent._plan(data, policy(), previous)
    start = data.calendar[plan["holdout_start"]]
    observation = report["observation"]
    assert observation["end"] == (start - timedelta(days=1)).isoformat()
    assert observation["observation_limit"] == {
        "end": observation["end"],
        "reason": "next_uncommitted_holdout",
        "next_holdout_start": start.isoformat(),
    }
    assert observation["exposure_recorded"] is True
    assert cohort_rows(store)[0]["registered_before_start"] == 0
    report = agent.tick("a", data, max_cycles=2)
    assert report["status"]["cycles"][1]["status"] == "COMPLETE"
    assert report["observation"]["exposure_recorded"] is True
    store.close()


def test_existing_exposure_rejects_final_atomically(tmp_path):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    plan = agent._plan(data, policy(), None)
    with store.transaction():
        agent._claim(
            policy(),
            data.sessions[plan["holdout_start"]],
            data.sessions[plan["holdout_end"]],
            stream(),
            "observation_exposure",
            data,
        )
    tables = (
        "agent_commitments",
        "agent_cohorts",
        "agent_budget_entries",
        "agent_intervals",
        "holdouts",
    )
    before = {
        t: [tuple(r) for r in store.db.execute(f"SELECT * FROM {t}")] for t in tables
    }
    with pytest.raises(AgentError, match="without a registered cohort"):
        agent.tick("a", data)
    assert before == {
        t: [tuple(r) for r in store.db.execute(f"SELECT * FROM {t}")] for t in tables
    }
    store.close()


@pytest.mark.parametrize("kind", ["holdout", "final_validation", "legacy", "import"])
def test_observation_rejects_final_without_cohort(tmp_path, kind):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    start, end = data.sessions[300], data.sessions[310]
    with store.transaction():
        if kind == "legacy":
            store.db.execute(
                "INSERT INTO holdouts VALUES(?,?,?,?)",
                ("OTHER", start.isoformat(), end.isoformat(), "legacy"),
            )
        else:
            agent._claim(
                policy(),
                start,
                end,
                "import:old" if kind == "import" else "old",
                "holdout" if kind == "import" else kind,
                data,
            )
    with pytest.raises(AgentError, match="outside its cohort"), store.transaction():
        agent._claim(policy(), start, end, stream(), "observation_exposure", data)
    assert not store.db.execute(
        "SELECT 1 FROM agent_intervals WHERE kind='observation_exposure'"
    ).fetchone()
    store.close()


@pytest.mark.parametrize(
    "first,second",
    [
        (a, b)
        for a in ("holdout", "shadow", "final_validation")
        for b in ("holdout", "shadow", "final_validation")
    ],
)
def test_exclusive_kinds_stay_exclusive(tmp_path, first, second):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    with store.transaction():
        agent._claim(
            policy(), data.sessions[300], data.sessions[310], "one", first, data
        )
    with (
        pytest.raises(
            AgentError, match="Evaluation interval overlaps consumed market evidence"
        ),
        store.transaction(),
    ):
        agent._claim(
            policy(), data.sessions[305], data.sessions[315], "two", second, data
        )
    store.close()


def test_other_study_cannot_reuse_cohort_final(tmp_path):
    data = market()
    store, agent, now = setup_agent(tmp_path, data)
    now[0] = session_time(data.sessions[180], 17)
    agent.tick("a", prefix(data, 180))
    agent.init("b", policy())
    with pytest.raises(
        AgentError, match="Evaluation interval overlaps consumed market evidence"
    ):
        agent.tick("b", prefix(data, 180))
    assert len(cohort_rows(store)) == 1
    store.close()


def test_exposure_failure_isolated_from_research(tmp_path):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    with store.transaction():
        agent._claim(
            policy(), data.sessions[287], data.sessions[288], "legacy", "holdout", data
        )
    report = agent.tick("a", data)
    assert report["cycles"][0]["research_champion_updated"]
    assert report["observation"]["status"] == "ok"
    assert report["observation"]["exposure_recorded"] is False
    assert "outside its cohort" in report["observation"]["exposure_error"]
    store.close()


def test_shadow_and_exposure_coexist_and_observation_read_is_pure(tmp_path):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    report = agent.tick("a", data)
    assert report["observation"]["exposure_recorded"] is True
    assert (
        store.db.execute(
            "SELECT count(*) FROM agent_intervals WHERE kind='observation_exposure'"
        ).fetchone()[0]
        == 1
    )
    assert not store.db.execute(
        "SELECT 1 FROM holdouts WHERE run_id=?", (stream(),)
    ).fetchone()
    before = list(store.db.iterdump())
    assert agent.observation_ledger("a", data) == report["observation"]
    assert list(store.db.iterdump()) == before
    store.close()


def test_legacy_intervals_are_not_backfilled(tmp_path):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    with store.transaction():
        agent._claim(
            policy(), data.sessions[10], data.sessions[20], "old-final", "holdout", data
        )
        agent._claim(
            policy(), data.sessions[21], data.sessions[30], "old-shadow", "shadow", data
        )
    before = [tuple(r) for r in store.db.execute("SELECT * FROM agent_intervals")]
    agent = ResearchAgent(store)
    assert agent.status("a")["cohorts"] == []
    assert cohort_rows(store) == []
    assert before == [
        tuple(r) for r in store.db.execute("SELECT * FROM agent_intervals")
    ]
    store.close()


def test_only_registered_stream_can_observe_final(tmp_path):
    data = market()
    store, agent, now = setup_agent(tmp_path, data)
    now[0] = session_time(data.sessions[180], 17)
    agent.tick("a", prefix(data, 180))
    cohort = cohort_rows(store)[0]
    start, end = date.fromisoformat(cohort["start"]), date.fromisoformat(cohort["end"])
    with pytest.raises(AgentError, match="outside its cohort"), store.transaction():
        agent._claim(
            policy(),
            start,
            end,
            "observation:KRX:other-family",
            "observation_exposure",
            data,
        )
    with store.transaction():
        agent._claim(policy(), start, end, stream(), "observation_exposure", data)
    assert (
        store.db.execute(
            "SELECT end FROM agent_intervals WHERE kind='observation_exposure'"
        ).fetchone()[0]
        == cohort["end"]
    )
    store.close()


def test_exposure_extension_preserves_original_start_and_checks_gap(tmp_path):
    data = market()
    store, agent, _ = setup_agent(tmp_path, data)
    with store.transaction():
        agent._claim(
            policy(),
            data.sessions[300],
            data.sessions[302],
            stream(),
            "observation_exposure",
            data,
        )
        agent._claim(
            policy(),
            data.sessions[299],
            data.sessions[303],
            stream(),
            "observation_exposure",
            data,
        )
        agent._claim(
            policy(), data.sessions[305], data.sessions[306], "old", "holdout", data
        )
    before = tuple(
        store.db.execute(
            "SELECT start,end FROM agent_intervals WHERE kind='observation_exposure'"
        ).fetchone()
    )
    assert before == (data.sessions[300].isoformat(), data.sessions[303].isoformat())
    with pytest.raises(AgentError, match="outside its cohort"), store.transaction():
        agent._claim(
            policy(),
            data.sessions[308],
            data.sessions[309],
            stream(),
            "observation_exposure",
            data,
        )
    assert (
        tuple(
            store.db.execute(
                "SELECT start,end FROM agent_intervals WHERE kind='observation_exposure'"
            ).fetchone()
        )
        == before
    )
    store.close()


@pytest.mark.parametrize("boundary", ["calendar", "budget"])
def test_observation_limit_skips_missing_calendar_or_exhausted_budget(
    tmp_path, boundary
):
    data = market()
    p = replace(policy(), max_validation_cycles=1) if boundary == "budget" else policy()
    store, agent, _ = setup_agent(tmp_path, data, p)
    report = agent.tick("a", data)
    if boundary == "calendar":
        # Drop only the unobserved future calendar, without changing prices.
        short = data.prefix(data.sessions[280])
        observation = agent.observation_ledger("a", short)
        assert observation["end"] == short.sessions[-1].isoformat()
    else:
        observation = report["observation"]
        assert report["status"]["budget"]["remaining"] == 0
        assert observation["end"] == data.sessions[-1].isoformat()
    assert observation["observation_limit"]["reason"] == "data_end"
    assert observation["observation_limit"]["next_holdout_start"] is None
    store.close()
